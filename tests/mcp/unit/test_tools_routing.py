from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from arie_mcp import db
from arie_mcp.errors import DbUnavailableError, NotFoundError
from arie_mcp.tools import routing

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


def _step_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "step_number": 1,
        "candidate_provider": "abstract_company_enrichment",
        "p_flips_decision": 0.4,
        "business_value": 10.0,
        "expected_cost": 0.00165,
        "latency_penalty": 0.001,
        "net_evoi": 3.9,
        "chosen": True,
        "confidence_before": 0.5,
        "confidence_after": 0.7,
        "created_at": _NOW,
    }
    row.update(overrides)
    return row


async def test_no_pool_raises_db_unavailable() -> None:
    with pytest.raises(DbUnavailableError):
        await routing.inspect_routing_decision_impl(None, uuid4(), statement_timeout_ms=5000)


async def test_no_decisions_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "run_query", lambda *a, **k: [])

    with pytest.raises(NotFoundError):
        await routing.inspect_routing_decision_impl(
            _fake_pool(), uuid4(), statement_timeout_ms=5000
        )


async def test_returns_ordered_steps_and_lead_context(monkeypatch: pytest.MonkeyPatch) -> None:
    lead_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_voi_decisions" in sql:
            return [_step_row(step_number=1, chosen=True), _step_row(step_number=2, chosen=False)]
        return [{"status": "AWAITING_HUMAN", "is_shadow": False}]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await routing.inspect_routing_decision_impl(
        _fake_pool(), lead_id, statement_timeout_ms=5000
    )

    assert outcome.data["lead_id"] == str(lead_id)
    assert outcome.data["lead_status"] == "AWAITING_HUMAN"
    assert outcome.data["lead_is_shadow"] is False
    assert [s["step_number"] for s in outcome.data["steps"]] == [1, 2]
    assert outcome.row_count == 2
