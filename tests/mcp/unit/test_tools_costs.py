from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from arie_mcp import db
from arie_mcp.errors import DbUnavailableError
from arie_mcp.tools import costs


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


async def test_no_pool_raises_db_unavailable() -> None:
    with pytest.raises(DbUnavailableError):
        await costs.get_enrichment_costs_impl(
            None,
            organization_id=None,
            provider=None,
            since=None,
            group_by="provider",
            statement_timeout_ms=5000,
        )


async def test_default_lookback_is_seven_days(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        captured["since"] = params["since"]
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    await costs.get_enrichment_costs_impl(
        _fake_pool(),
        organization_id=None,
        provider=None,
        since=None,
        group_by="provider",
        statement_timeout_ms=5000,
    )

    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs((captured["since"] - expected).total_seconds()) < 5


async def test_requested_since_older_than_90_days_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        captured["since"] = params["since"]
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    far_past = datetime.now(UTC) - timedelta(days=365)
    await costs.get_enrichment_costs_impl(
        _fake_pool(),
        organization_id=None,
        provider=None,
        since=far_past,
        group_by="provider",
        statement_timeout_ms=5000,
    )

    floor = datetime.now(UTC) - timedelta(days=costs.MAX_LOOKBACK_DAYS)
    assert captured["since"] > far_past
    assert abs((captured["since"] - floor).total_seconds()) < 5


async def test_a_recent_since_is_used_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        captured["since"] = params["since"]
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    recent = datetime.now(UTC) - timedelta(days=2)
    await costs.get_enrichment_costs_impl(
        _fake_pool(),
        organization_id=None,
        provider=None,
        since=recent,
        group_by="provider",
        statement_timeout_ms=5000,
    )

    assert captured["since"] == recent


async def test_group_by_day_uses_day_column(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        captured["sql"] = sql
        return []

    monkeypatch.setattr(db, "run_query", fake_run_query)

    await costs.get_enrichment_costs_impl(
        _fake_pool(),
        organization_id=None,
        provider=None,
        since=None,
        group_by="day",
        statement_timeout_ms=5000,
    )

    assert "GROUP BY day" in captured["sql"]


async def test_rows_are_shaped_correctly(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [
            {
                "group_key": "hunter_combined_enrichment",
                "cost_usd": 1.23,
                "credits_used": 4.5,
                "call_count": 10,
                "cache_hit_count": 2,
            }
        ]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await costs.get_enrichment_costs_impl(
        _fake_pool(),
        organization_id=None,
        provider=None,
        since=None,
        group_by="provider",
        statement_timeout_ms=5000,
    )

    assert outcome.row_count == 1
    row = outcome.data["rows"][0]
    assert row["group_key"] == "hunter_combined_enrichment"
    assert row["cost_usd"] == 1.23
    assert row["credits_used"] == 4.5
