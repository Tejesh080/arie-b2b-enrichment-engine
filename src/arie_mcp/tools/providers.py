"""``list_providers`` and ``inspect_provider_health`` (spec § 9.7, § 9.8).

``list_providers`` is process-static — no DB call — sourced directly from
``arie.live.providers``' own registry rather than a second, parallel list
(spec § 7.3 / the "do not duplicate provider definitions" instruction).
``inspect_provider_health`` reads raw timestamps/counts from ``mcp_diag``
views and applies ``arie.config.LIVE_STRATEGY.quota_cooldown_seconds`` —
the same window constant ``arie.live.cooldown.ProviderCooldownGuard`` uses —
rather than re-deriving that number.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie.config import LIVE_STRATEGY, RUNTIME
from arie.live.providers import LIVE_PROVIDER_NAMES, REGISTERED_LIVE_PROVIDER_NAMES
from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError, InvalidInputError


class ProviderInfo(BaseModel):
    name: str
    wired: bool
    """``True`` iff a live adapter is actually registered/callable
    (``REGISTERED_LIVE_PROVIDER_NAMES``) — a provider can exist in the
    billing-relevant superset (``LIVE_PROVIDER_NAMES``) with a defined
    contract before its adapter is wired (Apollo's own history, per that
    module's docstring)."""
    default_order_position: int | None
    """Position in the default cheapest-first acquisition order — only
    meaningful (non-null) for a wired provider."""


async def list_providers_impl() -> ToolOutcome:
    order_index = {name: i for i, name in enumerate(REGISTERED_LIVE_PROVIDER_NAMES)}
    providers = [
        ProviderInfo(
            name=name,
            wired=name in REGISTERED_LIVE_PROVIDER_NAMES,
            default_order_position=order_index.get(name),
        ).model_dump(mode="json")
        for name in LIVE_PROVIDER_NAMES
    ]
    data: dict[str, Any] = {
        "providers": providers,
        "process_provider_mode": RUNTIME.provider_mode,
        "order_override_configured": bool(LIVE_STRATEGY.provider_order),
        "order_override_raw": LIVE_STRATEGY.provider_order or None,
    }
    return ToolOutcome(data=data, row_count=len(providers))


class ProviderHealth(BaseModel):
    provider: str
    scope: Literal["organization", "global"]
    organization_id: UUID | None
    cooling_down: bool
    cooling_down_until: datetime | None
    last_quota_error_at: datetime | None
    call_count: int
    error_count: int
    error_rate: float | None
    cache_hit_count: int
    last_success_at: datetime | None
    last_call_at: datetime | None


def _filtered_sql(
    base_select: str, table: str, *, organization_id: UUID | None
) -> tuple[str, dict[str, Any]]:
    clauses = ["provider = %(provider)s"]
    params: dict[str, Any] = {}
    if organization_id is not None:
        clauses.append("organization_id = %(organization_id)s")
        params["organization_id"] = str(organization_id)
    sql = f"{base_select} FROM {table} WHERE " + " AND ".join(clauses)
    return sql, params


def _collect(
    pool: ConnectionPool, provider: str, organization_id: UUID | None, *, statement_timeout_ms: int
) -> ProviderHealth:
    params: dict[str, Any] = {"provider": provider}
    if organization_id is not None:
        params["organization_id"] = str(organization_id)

    quota_sql, quota_params = _filtered_sql(
        "SELECT last_quota_error_at",
        "mcp_diag.v_provider_quota_signal",
        organization_id=organization_id,
    )
    quota_params["provider"] = provider
    quota_rows = db.run_query(
        pool, quota_sql, quota_params, statement_timeout_ms=statement_timeout_ms
    )
    last_quota_error_at = max(
        (
            row["last_quota_error_at"]
            for row in quota_rows
            if row["last_quota_error_at"] is not None
        ),
        default=None,
    )

    activity_sql, activity_params = _filtered_sql(
        "SELECT call_count, error_count, cache_hit_count, last_success_at, last_call_at",
        "mcp_diag.v_provider_activity",
        organization_id=organization_id,
    )
    activity_params["provider"] = provider
    activity_rows = db.run_query(
        pool, activity_sql, activity_params, statement_timeout_ms=statement_timeout_ms
    )
    call_count = sum(row["call_count"] for row in activity_rows)
    error_count = sum(row["error_count"] for row in activity_rows)
    cache_hit_count = sum(row["cache_hit_count"] for row in activity_rows)
    last_success_at = max(
        (row["last_success_at"] for row in activity_rows if row["last_success_at"] is not None),
        default=None,
    )
    last_call_at = max(
        (row["last_call_at"] for row in activity_rows if row["last_call_at"] is not None),
        default=None,
    )

    cooling_down = False
    cooling_down_until: datetime | None = None
    window = LIVE_STRATEGY.quota_cooldown_seconds
    if window > 0 and last_quota_error_at is not None:
        candidate_until = last_quota_error_at + timedelta(seconds=window)
        if candidate_until > datetime.now(UTC):
            cooling_down = True
            cooling_down_until = candidate_until

    return ProviderHealth(
        provider=provider,
        scope="organization" if organization_id is not None else "global",
        organization_id=organization_id,
        cooling_down=cooling_down,
        cooling_down_until=cooling_down_until,
        last_quota_error_at=last_quota_error_at,
        call_count=call_count,
        error_count=error_count,
        error_rate=(error_count / call_count) if call_count else None,
        cache_hit_count=cache_hit_count,
        last_success_at=last_success_at,
        last_call_at=last_call_at,
    )


async def inspect_provider_health_impl(
    pool: ConnectionPool | None,
    provider: str,
    organization_id: UUID | None,
    *,
    statement_timeout_ms: int,
) -> ToolOutcome:
    if provider not in REGISTERED_LIVE_PROVIDER_NAMES:
        raise InvalidInputError(
            f"unknown provider {provider!r} — registered providers: "
            f"{list(REGISTERED_LIVE_PROVIDER_NAMES)}"
        )
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    health = await asyncio.to_thread(
        _collect, pool, provider, organization_id, statement_timeout_ms=statement_timeout_ms
    )
    return ToolOutcome(data=health.model_dump(mode="json"))
