from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from arie.billing.plans import PLAN_DEFINITIONS, UNSUBSCRIBED
from arie_mcp import db
from arie_mcp.errors import DbUnavailableError, NotFoundError
from arie_mcp.tools import configuration


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


async def test_process_configuration_never_contains_env_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_super_secret_value")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_super_secret_value")

    outcome = await configuration.inspect_configuration_impl(None, None, statement_timeout_ms=5000)

    blob = str(outcome.data)
    assert "sk_live_super_secret_value" not in blob
    assert "whsec_super_secret_value" not in blob
    assert outcome.data["process"]["categories"]["commercial_stripe"] is True


async def test_process_configuration_reports_false_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)

    outcome = await configuration.inspect_configuration_impl(None, None, statement_timeout_ms=5000)

    assert outcome.data["process"]["categories"]["commercial_stripe"] is False


async def test_partial_category_configuration_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # A category needs ALL its variables set to count as configured.
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)

    outcome = await configuration.inspect_configuration_impl(None, None, statement_timeout_ms=5000)

    assert outcome.data["process"]["categories"]["supabase_auth"] is False


async def test_no_organization_id_omits_organization_section() -> None:
    outcome = await configuration.inspect_configuration_impl(None, None, statement_timeout_ms=5000)
    assert outcome.data["organization"] is None


async def test_organization_id_without_pool_raises_db_unavailable() -> None:
    with pytest.raises(DbUnavailableError):
        await configuration.inspect_configuration_impl(None, uuid4(), statement_timeout_ms=5000)


async def test_unknown_organization_raises_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "run_query", lambda *a, **k: [])

    with pytest.raises(NotFoundError):
        await configuration.inspect_configuration_impl(
            _fake_pool(), uuid4(), statement_timeout_ms=5000
        )


async def test_internal_plan_resolves_to_internal_entitlements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_organization_summary" in sql:
            return [{"execution_mode": "simulated"}]
        return [{"plan": "internal", "status": "none"}]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await configuration.inspect_configuration_impl(
        _fake_pool(), org_id, statement_timeout_ms=5000
    )

    assert outcome.data["organization"]["entitlements"]["plan"] == "internal"
    assert outcome.data["organization"]["entitlements"] == PLAN_DEFINITIONS["internal"].__dict__


async def test_subscribed_paid_plan_resolves_to_its_own_entitlements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_organization_summary" in sql:
            return [{"execution_mode": "live_human_only"}]
        return [{"plan": "growth", "status": "active"}]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await configuration.inspect_configuration_impl(
        _fake_pool(), org_id, statement_timeout_ms=5000
    )

    assert outcome.data["organization"]["execution_mode"] == "live_human_only"
    assert outcome.data["organization"]["entitlements"] == PLAN_DEFINITIONS["growth"].__dict__


async def test_unpaid_plan_falls_back_to_unsubscribed(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_organization_summary" in sql:
            return [{"execution_mode": "simulated"}]
        return [{"plan": "growth", "status": "past_due"}]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await configuration.inspect_configuration_impl(
        _fake_pool(), org_id, statement_timeout_ms=5000
    )

    assert outcome.data["organization"]["entitlements"] == UNSUBSCRIBED.__dict__


async def test_no_billing_row_falls_back_to_unsubscribed(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_organization_summary" in sql:
            return [{"execution_mode": "simulated"}]
        return []  # no organization_billing row at all

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await configuration.inspect_configuration_impl(
        _fake_pool(), org_id, statement_timeout_ms=5000
    )

    assert outcome.data["organization"]["entitlements"] == UNSUBSCRIBED.__dict__


async def test_entitlements_never_contain_stripe_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()

    def fake_run_query(
        pool: object, sql: str, params: dict[str, Any], **kwargs: Any
    ) -> list[dict[str, Any]]:
        if "v_organization_summary" in sql:
            return [{"execution_mode": "simulated"}]
        return [{"plan": "pro", "status": "active"}]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await configuration.inspect_configuration_impl(
        _fake_pool(), org_id, statement_timeout_ms=5000
    )

    blob = str(outcome.data)
    assert "stripe_customer_id" not in blob
    assert "stripe_subscription_id" not in blob
    assert "cus_" not in blob  # Stripe customer id prefix
    assert "sub_" not in blob  # Stripe subscription id prefix
