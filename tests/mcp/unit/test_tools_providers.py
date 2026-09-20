from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
from arie_mcp import db
from arie_mcp.errors import DbUnavailableError, InvalidInputError
from arie_mcp.tools import providers


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


async def test_list_providers_reports_the_real_registry() -> None:
    outcome = await providers.list_providers_impl()

    names = [p["name"] for p in outcome.data["providers"]]
    assert names[:3] == list(REGISTERED_LIVE_PROVIDER_NAMES)
    assert all(p["wired"] for p in outcome.data["providers"][:3])
    assert outcome.row_count == len(outcome.data["providers"])


async def test_inspect_provider_health_rejects_unknown_provider() -> None:
    with pytest.raises(InvalidInputError):
        await providers.inspect_provider_health_impl(
            _fake_pool(), "not_a_real_provider", None, statement_timeout_ms=5000
        )


async def test_inspect_provider_health_no_pool_raises_db_unavailable() -> None:
    provider = REGISTERED_LIVE_PROVIDER_NAMES[0]
    with pytest.raises(DbUnavailableError):
        await providers.inspect_provider_health_impl(
            None, provider, None, statement_timeout_ms=5000
        )


async def test_reports_cooling_down_within_window(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = REGISTERED_LIVE_PROVIDER_NAMES[0]
    recent_quota_error = datetime.now(UTC) - timedelta(minutes=5)

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_provider_quota_signal" in sql:
            return [{"last_quota_error_at": recent_quota_error}]
        return [
            {
                "call_count": 10,
                "error_count": 2,
                "cache_hit_count": 3,
                "last_success_at": recent_quota_error,
                "last_call_at": recent_quota_error,
            }
        ]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    health = await providers.inspect_provider_health_impl(
        _fake_pool(), provider, None, statement_timeout_ms=5000
    )

    assert health.data["cooling_down"] is True
    assert health.data["cooling_down_until"] is not None
    assert health.data["call_count"] == 10
    assert health.data["error_rate"] == 0.2
    assert health.data["scope"] == "global"


async def test_not_cooling_down_when_quota_error_is_old(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = REGISTERED_LIVE_PROVIDER_NAMES[0]
    old_quota_error = datetime.now(UTC) - timedelta(days=10)

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_provider_quota_signal" in sql:
            return [{"last_quota_error_at": old_quota_error}]
        return [
            {
                "call_count": 0,
                "error_count": 0,
                "cache_hit_count": 0,
                "last_success_at": None,
                "last_call_at": None,
            }
        ]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    health = await providers.inspect_provider_health_impl(
        _fake_pool(), provider, None, statement_timeout_ms=5000
    )

    assert health.data["cooling_down"] is False
    assert health.data["cooling_down_until"] is None
    assert health.data["last_quota_error_at"] is not None  # still reported, just not "current"
    assert health.data["error_rate"] is None  # no calls at all


async def test_organization_scope_is_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = REGISTERED_LIVE_PROVIDER_NAMES[0]
    org_id = uuid4()

    monkeypatch.setattr(db, "run_query", lambda *a, **k: [])

    health = await providers.inspect_provider_health_impl(
        _fake_pool(), provider, org_id, statement_timeout_ms=5000
    )

    assert health.data["scope"] == "organization"
    assert health.data["organization_id"] == str(org_id)
