"""Live-database tests for `arie.llm.deepseek.record_extraction_cost`.

No live DeepSeek call anywhere in this file — `ExtractionOutcome` is
constructed directly, the same fixture data `tests/unit/test_llm_deepseek_client.py`
uses. What's under test here is the *other* half:  does the ledger really end
up with the right rows, priced correctly, idempotent on retry, once a real
Postgres connection is involved. The mocked-HTTP unit tests already cover the
"only bill request_succeeded attempts" logic in isolation; this confirms it
against the real `PostgresCostLedger`, not a stand-in.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg
import pytest
from tests.integration.conftest import IngestCleanup

from arie.ledger.store import PostgresCostLedger
from arie.llm.deepseek import PURPOSE, ExtractionAttempt, ExtractionOutcome, record_extraction_cost

pytestmark = pytest.mark.integration


def _outcome(*attempts: ExtractionAttempt) -> ExtractionOutcome:
    return ExtractionOutcome(model="deepseek-chat", signal=None, attempts=attempts)


def _billable(
    prompt_tokens: int, completion_tokens: int, *, error: str | None = None
) -> ExtractionAttempt:
    return ExtractionAttempt(
        request_succeeded=True,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=250.0,
        validation_error=error,
    )


def _network_failure() -> ExtractionAttempt:
    return ExtractionAttempt(
        request_succeeded=False,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=1.0,
        validation_error="request failed: timeout",
    )


def test_a_successful_single_attempt_lands_as_one_priced_row(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    key_base = f"test-extract-{uuid.uuid4().hex}"
    cleanup_ingest.model_call_keys.append(f"{key_base}:attempt0")

    outcome = _outcome(_billable(1000, 100))
    writes = record_extraction_cost(cost_ledger, outcome, idempotency_key_base=key_base)

    assert len(writes) == 1
    # 1000 * 0.27/1e6 + 100 * 1.10/1e6, from arie.ledger.pricing.MODEL_PRICES
    assert writes[0].cost_usd == Decimal("0.00038")

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT model, tier, purpose, prompt_tokens, completion_tokens "
            "FROM model_calls WHERE idempotency_key = %s",
            (f"{key_base}:attempt0",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row == ("deepseek-chat", "cheap", PURPOSE, 1000, 100)


def test_a_failed_request_attempt_is_never_billed(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    key_base = f"test-extract-{uuid.uuid4().hex}"
    cleanup_ingest.model_call_keys.append(f"{key_base}:attempt1")

    outcome = _outcome(_network_failure(), _billable(500, 50))
    writes = record_extraction_cost(cost_ledger, outcome, idempotency_key_base=key_base)

    assert len(writes) == 1, "only the billable second attempt should reach the ledger"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM model_calls WHERE idempotency_key LIKE %s", (f"{key_base}:%",)
        )
        count = cur.fetchone()
    assert count is not None and count[0] == 1


def test_a_retry_that_still_failed_validation_is_billed_separately_from_the_success(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The whole point of per-attempt ledgering: a validation-failing retry
    still cost real money and must show up as its own row, not be silently
    merged into or replaced by the attempt that eventually succeeded."""
    key_base = f"test-extract-{uuid.uuid4().hex}"
    cleanup_ingest.model_call_keys.extend([f"{key_base}:attempt0", f"{key_base}:attempt1"])

    outcome = _outcome(
        _billable(300, 20, error="schema mismatch"),
        _billable(310, 45),
    )
    writes = record_extraction_cost(cost_ledger, outcome, idempotency_key_base=key_base)

    assert len(writes) == 2
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT prompt_tokens, completion_tokens FROM model_calls "
            "WHERE idempotency_key LIKE %s ORDER BY idempotency_key",
            (f"{key_base}:%",),
        )
        rows = cur.fetchall()
    assert rows == [(300, 20), (310, 45)]


def test_recording_the_same_outcome_twice_does_not_double_bill(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Mirrors the guarantee `tests/integration/test_cost_ledger_integration.py`
    already pins for the ledger itself: a caller that crashes after recording
    and retries the whole operation must not pay twice."""
    key_base = f"test-extract-{uuid.uuid4().hex}"
    cleanup_ingest.model_call_keys.append(f"{key_base}:attempt0")
    outcome = _outcome(_billable(200, 40))

    first = record_extraction_cost(cost_ledger, outcome, idempotency_key_base=key_base)
    second = record_extraction_cost(cost_ledger, outcome, idempotency_key_base=key_base)

    assert first[0].recorded is True
    assert second[0].recorded is False
    assert second[0].call_id == first[0].call_id

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM model_calls WHERE idempotency_key = %s", (f"{key_base}:attempt0",)
        )
        count = cur.fetchone()
    assert count is not None and count[0] == 1


def test_extraction_cost_is_attributable_to_a_lead(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """`lead_id` flows through to `v_lead_cost` — the same rollup Step 9 wired
    `GET /leads/{id}` to read from — so a lead's page reflects LLM spend too,
    not only provider spend."""
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO leads (source) VALUES ('llm-ledger-test') RETURNING lead_id")
        row = cur.fetchone()
    assert row is not None
    lead_id = row[0]
    db_conn.commit()
    cleanup_ingest.lead_ids.append(lead_id)

    key_base = f"test-extract-{uuid.uuid4().hex}"
    cleanup_ingest.model_call_keys.append(f"{key_base}:attempt0")
    outcome = _outcome(_billable(1000, 100))
    record_extraction_cost(cost_ledger, outcome, lead_id=lead_id, idempotency_key_base=key_base)

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.model_cost_usd == Decimal("0.00038")
    assert cost.provider_cost_usd == Decimal(0)
