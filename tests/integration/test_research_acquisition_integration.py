"""Live-database tests for M7 Slice 5's research planner/executor.

Drives a real corpus lead through ingestion -> queue -> compute_score exactly
like `test_receipt_integration.py`, so `arie.research_acquisition` operates on
a genuinely resolved company/person identity — required for
`execute_research`'s synthesized-evidence path, which needs a real canonical
domain/email to key observations by. The AUTONOMOUS_EMAIL identity naturally
lands well above the qualify threshold; the "material unknown" scenarios
manually adjust that one lead's own `decision_receipts` row afterward
(never a second lead) to engineer a controlled borderline score without
depending on which exact corpus row happens to be borderline this seed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.evalgen.schema import EvalLead
from arie.evidence.store import PostgresEvidenceStore
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import JobHandler, run_worker_cycle
from arie.ledger.store import PostgresCostLedger
from arie.organizations import SIMULATED
from arie.research import ResearchReasonCode, ResearchTargetField
from arie.research_acquisition import build_research_plan, execute_research
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration

_MAX_CYCLES = 6
AUTONOMOUS_EMAIL = "nadia.delacroix@lumen500.com"
AUTONOMOUS_DOMAIN = "lumen500.com"


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def research_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=6, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def handlers(
    runtime: SimulatedEnrichmentRuntime, research_pool: ConnectionPool
) -> dict[str, JobHandler]:
    return build_handlers(research_pool, runtime=runtime, provider_mode="simulated")


def _ingest_and_decide(
    api_client: TestClient,
    cleanup: IngestCleanup,
    job_queue: PostgresJobQueue,
    research_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
) -> dict[str, Any]:
    cleanup.domains.append(AUTONOMOUS_DOMAIN)
    cleanup.emails.append(AUTONOMOUS_EMAIL)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("research"),
            "email": AUTONOMOUS_EMAIL,
            "external_ref": f"research-{uuid.uuid4().hex[:12]}",
            "company_domain": AUTONOMOUS_DOMAIN,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))

    for _ in range(_MAX_CYCLES):
        run_worker_cycle(
            job_queue,
            research_pool,
            handlers,
            worker_id=f"research-it-{uuid.uuid4().hex[:8]}",
            batch_size=3,
            job_types=["compute_score"],
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT status FROM jobs WHERE job_id = %s", (body["job_id"],))
            row = cur.fetchone()
        assert row is not None
        if row[0] not in ("pending", "processing"):
            break
    return body


def _make_borderline(db_conn: psycopg.Connection, lead_id: str, *, unknown_field: str) -> None:
    """Overwrite this lead's own (already-written) receipt with a controlled
    borderline score and exactly one unknown field — never a second lead, and
    never touched before the real pipeline already produced one real row."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE decision_receipts
            SET score_value = 60, score_lower = 60, score_upper = 80,
                evidence_snapshot = jsonb_set(
                    jsonb_set(evidence_snapshot, '{unknown}', %s::jsonb),
                    '{known}',
                    (
                        SELECT COALESCE(jsonb_agg(item), '[]'::jsonb)
                        FROM jsonb_array_elements(evidence_snapshot->'known') item
                        WHERE item->>'field' != %s
                    )
                )
            WHERE lead_id = %s
            """,
            (Jsonb([unknown_field]), unknown_field, lead_id),
        )
    db_conn.commit()


def test_decision_already_clear_needs_no_research(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    job_queue: PostgresJobQueue,
    research_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cost_ledger: PostgresCostLedger,
) -> None:
    lead = _ingest_and_decide(
        api_client, cleanup_ingest, job_queue, research_pool, handlers, db_conn
    )
    plan = build_research_plan(
        db_conn,
        cost_ledger,
        organization_id=ORG,
        lead_id=uuid.UUID(lead["lead_id"]),
        execution_mode=SIMULATED,
        llm=None,
        now=datetime.now(UTC),
    )
    assert plan is not None
    assert plan.approved is False
    assert plan.reason_code is ResearchReasonCode.DECISION_ALREADY_CLEAR


def test_material_unknown_is_approved_and_executes(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    job_queue: PostgresJobQueue,
    research_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    cost_ledger: PostgresCostLedger,
    evidence_store: PostgresEvidenceStore,
) -> None:
    now = datetime.now(UTC)
    lead = _ingest_and_decide(
        api_client, cleanup_ingest, job_queue, research_pool, handlers, db_conn
    )
    lead_id = uuid.UUID(lead["lead_id"])
    _make_borderline(db_conn, lead["lead_id"], unknown_field="employee_count")

    plan = build_research_plan(
        db_conn,
        cost_ledger,
        organization_id=ORG,
        lead_id=lead_id,
        execution_mode=SIMULATED,
        llm=None,
        now=now,
    )
    assert plan is not None
    assert plan.target_field is ResearchTargetField.EMPLOYEE_COUNT
    assert plan.approved is True
    assert plan.reason_code is ResearchReasonCode.RESEARCH_APPROVED

    result = execute_research(
        db_conn,
        cost_ledger,
        evidence_store,
        organization_id=ORG,
        lead_id=lead_id,
        target_field=ResearchTargetField.EMPLOYEE_COUNT,
        execution_mode=SIMULATED,
        now=now,
    )
    assert result is not None
    assert result.approved is True
    assert result.preview is not None

    # Idempotent retry: the second call must not spend again.
    retry = execute_research(
        db_conn,
        cost_ledger,
        evidence_store,
        organization_id=ORG,
        lead_id=lead_id,
        target_field=ResearchTargetField.EMPLOYEE_COUNT,
        execution_mode=SIMULATED,
        now=now,
    )
    assert retry is not None
    assert retry.cost_usd == Decimal(0)


def test_unknown_lead_returns_none(
    db_conn: psycopg.Connection, cost_ledger: PostgresCostLedger
) -> None:
    plan = build_research_plan(
        db_conn,
        cost_ledger,
        organization_id=ORG,
        lead_id=uuid.uuid4(),
        execution_mode=SIMULATED,
        llm=None,
        now=datetime.now(UTC),
    )
    assert plan is None
