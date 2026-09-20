"""``inspect_configuration`` (spec § 9.11).

Two independent sections. **Process configuration**: boolean presence of
env-var categories mirroring ``.env.example``'s own groupings — never a
value, secret or otherwise; a caller learns "is Stripe configured" (yes/no),
never a key. **Organization configuration** (only when ``organization_id``
is given): ``execution_mode`` (a database column, not a secret) and
resolved entitlements — reusing the *actual* entitlement model
(``arie.billing.plans.PLAN_DEFINITIONS``/``UNSUBSCRIBED`` and
``arie.billing.models.SUBSCRIBED_STATUSES``), not a second, invented one.
The read itself is re-derived against ``mcp_diag.v_organization_billing_summary``
rather than calling ``resolve_organization_entitlements`` directly, because
that function queries the bare ``organization_billing`` table (which
carries ``stripe_customer_id``/``stripe_subscription_id`` —
``arie_mcp_readonly`` has no grant there, and this view deliberately
excludes both columns at the schema layer regardless).
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie.billing.models import SUBSCRIBED_STATUSES
from arie.billing.plans import PLAN_DEFINITIONS, UNSUBSCRIBED, EffectiveEntitlements
from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError, NotFoundError

_CATEGORY_VARS: dict[str, tuple[str, ...]] = {
    "database": ("DATABASE_URL",),
    "supabase_auth": ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"),
    "provider_abstract": ("ABSTRACT_COMPANY_API_KEY",),
    "provider_hunter": ("HUNTER_API_KEY",),
    "provider_apollo": ("APOLLO_API_KEY",),
    "llm_deepseek": ("DEEPSEEK_API_KEY",),
    "llm_general": ("LLM_API_KEY",),
    "observability_otel": ("OTEL_EXPORTER_OTLP_ENDPOINT",),
    "observability_langfuse": ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"),
    "commercial_stripe": ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),
    "email": ("AHASEND_API_KEY",),
    "turnstile": ("TURNSTILE_SECRET_KEY", "TURNSTILE_SITE_KEY"),
    "firecrawl": ("FIRECRAWL_API_KEY",),
}
"""Category -> the env var name(s) whose *presence* (never value) decides
it. Mirrors .env.example's own real variable names — verified against that
file, not guessed."""


class ProcessConfiguration(BaseModel):
    categories: dict[str, bool]


class OrganizationConfiguration(BaseModel):
    organization_id: UUID
    execution_mode: str
    entitlements: EffectiveEntitlements


class Configuration(BaseModel):
    process: ProcessConfiguration
    organization: OrganizationConfiguration | None


def _process_configuration() -> ProcessConfiguration:
    categories = {
        category: all(bool(os.getenv(var)) for var in variables)
        for category, variables in _CATEGORY_VARS.items()
    }
    return ProcessConfiguration(categories=categories)


def _resolve_entitlements(plan: str | None, status: str | None) -> EffectiveEntitlements:
    """Mirrors `arie.billing.plans.resolve_organization_entitlements`'s own
    branch exactly, reusing its real constants — no entitlement number here
    is invented. Re-derived (not called directly) only because that
    function reads the bare `organization_billing` table, which
    `arie_mcp_readonly` has no grant on (spec § 7.3)."""
    if plan is None:
        return UNSUBSCRIBED
    if plan == "internal":
        return PLAN_DEFINITIONS["internal"]
    if status in SUBSCRIBED_STATUSES:
        return PLAN_DEFINITIONS[plan]
    return UNSUBSCRIBED


def _collect_organization(
    pool: ConnectionPool, organization_id: UUID, *, statement_timeout_ms: int
) -> OrganizationConfiguration:
    org_rows = db.run_query(
        pool,
        "SELECT execution_mode FROM mcp_diag.v_organization_summary WHERE organization_id = %(organization_id)s",
        {"organization_id": str(organization_id)},
        statement_timeout_ms=statement_timeout_ms,
    )
    if not org_rows:
        raise NotFoundError(f"no organization found with organization_id={organization_id}")

    billing_rows = db.run_query(
        pool,
        "SELECT plan, status FROM mcp_diag.v_organization_billing_summary WHERE organization_id = %(organization_id)s",
        {"organization_id": str(organization_id)},
        statement_timeout_ms=statement_timeout_ms,
    )
    plan = billing_rows[0]["plan"] if billing_rows else None
    status = billing_rows[0]["status"] if billing_rows else None

    return OrganizationConfiguration(
        organization_id=organization_id,
        execution_mode=org_rows[0]["execution_mode"],
        entitlements=_resolve_entitlements(plan, status),
    )


async def inspect_configuration_impl(
    pool: ConnectionPool | None, organization_id: UUID | None, *, statement_timeout_ms: int
) -> ToolOutcome:
    process = _process_configuration()

    organization: OrganizationConfiguration | None = None
    if organization_id is not None:
        if pool is None:
            raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")
        organization = await asyncio.to_thread(
            _collect_organization, pool, organization_id, statement_timeout_ms=statement_timeout_ms
        )

    configuration = Configuration(process=process, organization=organization)
    return ToolOutcome(data=configuration.model_dump(mode="json"))
