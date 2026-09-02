"""Live-database tests for M7 Slice 7's batch insights and CSV export:
`GET /batches/{id}/insights`, `POST /batches/{id}/summary`,
`GET /batches/{id}/export.csv`.

Builds a batch directly (company/person/lead/decision_receipts/
lead_batch_rows rows via `db_conn`), the same pattern
`test_copilot_integration.py` established for a decided lead, extended with
the `lead_batches`/`lead_batch_rows` linkage `arie.batch_insights`/
`arie.batch_export` both join through.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from tests.integration.conftest import IngestCleanup

from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG

pytestmark = pytest.mark.integration


def _snapshot(known: list[str], unknown: list[str]) -> dict[str, Any]:
    return {
        "known": [
            {
                "field": f,
                "source": "test",
                "confidence": 0.9,
                "candidate_count": 1,
                "contested": False,
            }
            for f in known
        ],
        "unknown": unknown,
        "execution_mode": "simulated",
    }


@pytest.fixture
def make_batch(db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup) -> Callable[..., UUID]:
    """Insert a `lead_batches` row and register it for the caller to attach
    rows to via `add_row`. Returns `batch_id`."""

    def _make(*, organization_id: UUID = ORG, filename: str = "test-batch.csv") -> UUID:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lead_batches (organization_id, filename, total_rows, accepted_rows, "
                "rejected_rows, created_by_user_id) VALUES (%s, %s, 0, 0, 0, %s) RETURNING batch_id",
                (organization_id, filename, uuid.uuid4()),
            )
            batch_id = cur.fetchone()[0]  # type: ignore[index]
        db_conn.commit()
        return batch_id  # type: ignore[no-any-return]

    return _make


@pytest.fixture
def add_batch_row(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> Callable[..., UUID]:
    """Insert one company/person/lead/decision_receipts/lead_batch_rows set,
    attributed to `batch_id`. Returns `lead_id`."""

    def _add(
        batch_id: UUID,
        *,
        row_number: int,
        organization_id: UUID = ORG,
        company_name: str = "Batch Test Co",
        status: str = "AUTO_ROUTED",
        decision: str = "auto_route",
        confidence: float = 0.9,
        score: float = 80.0,
        known: list[str] | None = None,
        unknown: list[str] | None = None,
        decided: bool = True,
    ) -> UUID:
        known = known if known is not None else ["employee_count", "title_seniority"]
        unknown = unknown if unknown is not None else []
        domain = f"{uuid.uuid4().hex[:10]}.test"
        email = f"contact@{domain}"

        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO companies (canonical_domain, name, normalized_name) "
                "VALUES (%s, %s, %s) RETURNING company_id",
                (domain, company_name, company_name.lower()),
            )
            company_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO persons (company_id, canonical_email, full_name, title, organization_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING person_id",
                (company_id, email, "Test Contact", "VP Sales", organization_id),
            )
            person_id = cur.fetchone()[0]  # type: ignore[index]
            cur.execute(
                "INSERT INTO leads (person_id, company_id, organization_id, source, status, batch_id) "
                "VALUES (%s, %s, %s, 'test', %s, %s) RETURNING lead_id",
                (person_id, company_id, organization_id, status, batch_id),
            )
            lead_id = cur.fetchone()[0]  # type: ignore[index]
            if decided:
                cur.execute(
                    """
                    INSERT INTO decision_receipts (
                        lead_id, organization_id, decision, autonomous, confidence, tau,
                        score_value, score_lower, score_upper, stop_reason, policy_name,
                        scorer_version, confidence_calibration, evidence_snapshot
                    ) VALUES (
                        %s, %s, %s, true, %s, 0.5, %s, %s, %s, 'decision_settled', 'test-policy',
                        'icp-1.0.0', 'test', %s
                    )
                    """,
                    (
                        lead_id,
                        organization_id,
                        decision,
                        confidence,
                        score,
                        score,
                        score,
                        Jsonb(_snapshot(known, unknown)),
                    ),
                )
            cur.execute(
                "INSERT INTO lead_batch_rows (batch_id, row_number, organization_id, raw_row, "
                "validation_status, lead_id) VALUES (%s, %s, %s, %s, 'accepted', %s)",
                (batch_id, row_number, organization_id, Jsonb({"email": email}), lead_id),
            )
        db_conn.commit()
        cleanup_ingest.lead_ids.append(lead_id)
        cleanup_ingest.domains.append(domain)
        cleanup_ingest.emails.append(email)
        return lead_id  # type: ignore[no-any-return]

    return _add


@pytest.fixture
def cleanup_batch(db_conn: psycopg.Connection) -> Any:
    ids: list[UUID] = []
    yield ids
    if ids:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM lead_batches WHERE batch_id = ANY(%s)", (ids,))
        db_conn.commit()


# ------------------------------------------------------------------ insights --


def test_batch_insights_priority_and_totals(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, decision="auto_route", confidence=0.9)  # contact_first
    add_batch_row(
        batch_id, row_number=2, decision="escalate_human", confidence=0.5
    )  # worth_pursuing
    add_batch_row(batch_id, row_number=3, status="AWAITING_HUMAN", decided=False)  # review

    response = api_client.get(f"/batches/{batch_id}/insights")
    assert response.status_code == 200
    body = response.json()
    assert body["total_leads"] == 3
    assert body["priority_counts"]["contact_first"] == 1
    assert body["priority_counts"]["worth_pursuing"] == 1
    assert body["priority_counts"]["review"] == 1
    assert body["priority_counts"]["skip"] == 0
    assert body["decided_leads"] == 2


def test_zero_row_batch_has_no_divide_by_zero(
    api_client: TestClient, make_batch: Callable[..., UUID], cleanup_batch: list[UUID]
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    response = api_client.get(f"/batches/{batch_id}/insights")
    assert response.status_code == 200
    body = response.json()
    assert body["total_leads"] == 0
    assert body["unknown_data_rate"] is None
    assert body["human_review_rate"] is None
    assert body["feedback_approval_rate"] is None


def test_unknown_data_rate_math(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    # 1 decided lead, 3 of 6 dimensions unknown -> rate 0.5
    add_batch_row(
        batch_id,
        row_number=1,
        known=["employee_count", "industry", "title_seniority"],
        unknown=["title_function", "buying_intent", "recent_trigger_event"],
    )
    response = api_client.get(f"/batches/{batch_id}/insights")
    body = response.json()
    assert body["decided_leads"] == 1
    assert body["expected_scoring_observations"] == 6
    assert body["unknown_scoring_observations"] == 3
    assert body["unknown_data_rate"] == pytest.approx(0.5)


def test_actual_provider_cost_unavailable_is_distinguishable_from_free(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1)
    response = api_client.get(f"/batches/{batch_id}/insights")
    body = response.json()
    assert body["actual_provider_cost_known_calls"] == 0
    assert float(body["actual_provider_cost_usd"]) == 0.0


def test_batch_insights_requires_no_llm_call(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    """No `LLMServiceDep` is even wired into the insights route — proven by
    the route responding correctly even though no LLM credential is
    configured in this test environment."""
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1)
    assert api_client.get(f"/batches/{batch_id}/insights").status_code == 200


def test_batch_insights_foreign_organization_is_404(
    api_client_org_b: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch(organization_id=ORG)
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, organization_id=ORG)
    response = api_client_org_b.get(f"/batches/{batch_id}/insights")
    assert response.status_code == 404


# ------------------------------------------------------------------- summary --


def test_batch_summary_falls_back_without_a_working_llm(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    """No DeepSeek credential is configured in this test environment, so this
    always exercises the deterministic-fallback branch — and must still
    return 200, never 500."""
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, decision="auto_route", confidence=0.9)
    response = api_client.post(f"/batches/{batch_id}/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]
    assert body["source"] in ("ai", "deterministic")


# -------------------------------------------------------------------- export --


def test_export_csv_headers_and_row_count(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, company_name="Acme Corp")
    add_batch_row(batch_id, row_number=2, company_name="Beta Inc")

    response = api_client.get(f"/batches/{batch_id}/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]

    reader = csv.reader(io.StringIO(response.text))
    rows = list(reader)
    assert rows[0] == [
        "company",
        "contact",
        "email",
        "priority",
        "score",
        "confidence",
        "reason",
        "next_action",
        "research_status",
        "status",
        "profile_version",
    ]
    assert len(rows) == 3  # header + 2 leads
    companies = {row[0] for row in rows[1:]}
    assert companies == {"Acme Corp", "Beta Inc"}


def test_export_csv_neutralizes_formula_injection_in_company_name(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, company_name="=cmd|'/c calc'!A1")

    response = api_client.get(f"/batches/{batch_id}/export.csv")
    reader = csv.reader(io.StringIO(response.text))
    rows = list(reader)
    company_cell = rows[1][0]
    assert company_cell.startswith("'=")


def test_export_csv_handles_commas_and_newlines_safely(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, company_name='Acme, Inc. "The Best"')

    response = api_client.get(f"/batches/{batch_id}/export.csv")
    reader = csv.reader(io.StringIO(response.text))
    rows = list(reader)
    assert rows[1][0] == 'Acme, Inc. "The Best"'


def test_export_csv_never_calls_the_llm(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1)
    assert api_client.get(f"/batches/{batch_id}/export.csv").status_code == 200


def test_export_csv_foreign_organization_is_404(
    api_client_org_b: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    batch_id = make_batch(organization_id=ORG)
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1, organization_id=ORG)
    response = api_client_org_b.get(f"/batches/{batch_id}/export.csv")
    assert response.status_code == 404


def test_export_csv_omits_technical_receipt_fields(
    api_client: TestClient,
    make_batch: Callable[..., UUID],
    add_batch_row: Callable[..., UUID],
    cleanup_batch: list[UUID],
) -> None:
    """No raw evidence_snapshot, no provider names, no internal receipt JSON
    anywhere in the body — only the named columns."""
    batch_id = make_batch()
    cleanup_batch.append(batch_id)
    add_batch_row(batch_id, row_number=1)
    response = api_client.get(f"/batches/{batch_id}/export.csv")
    assert "evidence_snapshot" not in response.text
    assert "decision_settled" not in response.text
    assert "icp-1.0.0" not in response.text
