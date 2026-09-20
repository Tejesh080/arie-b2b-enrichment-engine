-- =============================================================================
-- 0040_mcp_diag_slice2.sql — MCP Engineering Interface V0.1 slice 2
-- (specs/mcp-engineering-interface.md), adding exactly the mcp_diag objects
-- `get_recent_errors`, `list_providers`, `inspect_provider_health`,
-- `get_enrichment_costs`, `inspect_routing_decision`, and
-- `inspect_configuration` need — nothing more.
--
-- A new, additive migration rather than editing 0039 — that migration is
-- an accepted checkpoint (slice 1 is committed and reviewed); every future
-- mcp_diag object gets its own migration, the same one-thing-at-a-time
-- discipline this repo already follows for provider_calls (0010, 0011,
-- 0028, 0029).
--
-- Same security shape as 0039: one explicit GRANT SELECT per view, no
-- ALTER DEFAULT PRIVILEGES. No view here selects `persons`, `companies`,
-- `evidence.value`/`raw_ref`, `provider_calls.raw_response_ref`,
-- `organization_provider_configs.vault_secret_id`, `organization_api_keys`
-- (hashes), or anything in `vault.*` — structurally unreachable, not
-- filtered after the fact.
--
-- Must stay re-runnable against a database that already has it (ADR 0005).
-- =============================================================================

-- get_recent_errors, inspect_provider_health: recent provider-call failures
-- and suppressions. Deliberately excludes `raw_response_ref` (a pointer to a
-- stored raw vendor response, which may itself carry PII) and `entity_id`
-- is kept as an opaque UUID only (no join to persons/companies).
CREATE OR REPLACE VIEW mcp_diag.v_provider_call_errors AS
SELECT
    call_id,
    lead_id,
    organization_id,
    provider,
    entity_type,
    entity_id,
    error_kind,
    suppressed_reason,
    status,
    cache_hit,
    requested_at,
    completed_at,
    latency_ms
FROM public.provider_calls
WHERE error_kind IS NOT NULL OR suppressed_reason IS NOT NULL;

GRANT SELECT ON mcp_diag.v_provider_call_errors TO arie_mcp_readonly;

-- inspect_provider_health: the exact signal `arie.live.cooldown
-- .ProviderCooldownGuard.cooling_down_until` reads, re-exposed for
-- diagnostics — the cooldown *window* (LIVE_STRATEGY.quota_cooldown_seconds)
-- is applied in Python against this raw timestamp (spec § 7.3: reuse the
-- policy constant, not a second copy of it in SQL).
CREATE OR REPLACE VIEW mcp_diag.v_provider_quota_signal AS
SELECT
    provider,
    organization_id,
    max(completed_at) AS last_quota_error_at
FROM public.provider_calls
WHERE error_kind IN ('quota_exhausted', 'insufficient_credits')
GROUP BY provider, organization_id;

GRANT SELECT ON mcp_diag.v_provider_quota_signal TO arie_mcp_readonly;

-- inspect_provider_health: recent activity counts (success/error/cache-hit)
-- per provider+organization, for an error-rate figure — separate from
-- v_provider_call_errors so a healthy provider with zero errors still has
-- a row (an aggregate with no WHERE-clause survivors would otherwise
-- silently vanish it).
CREATE OR REPLACE VIEW mcp_diag.v_provider_activity AS
SELECT
    provider,
    organization_id,
    count(*)                                            AS call_count,
    count(*) FILTER (WHERE error_kind IS NOT NULL)       AS error_count,
    count(*) FILTER (WHERE cache_hit)                    AS cache_hit_count,
    max(completed_at) FILTER (WHERE error_kind IS NULL)  AS last_success_at,
    max(completed_at)                                    AS last_call_at
FROM public.provider_calls
GROUP BY provider, organization_id;

GRANT SELECT ON mcp_diag.v_provider_activity TO arie_mcp_readonly;

-- get_enrichment_costs: provider-level cost rollup. `cost_usd` is ARIE's own
-- modelled acquisition cost (migration 0010's docstring); `credits_used` is
-- the vendor's own metering unit where it applies. No lead-level breakdown
-- here — that stays bounded regardless of lead volume (spec § 9.9).
CREATE OR REPLACE VIEW mcp_diag.v_cost_rollup AS
SELECT
    provider,
    organization_id,
    date_trunc('day', requested_at) AS day,
    sum(cost_usd)                          AS cost_usd,
    sum(credits_used)                      AS credits_used,
    count(*) FILTER (WHERE NOT cache_hit)  AS call_count,
    count(*) FILTER (WHERE cache_hit)      AS cache_hit_count
FROM public.provider_calls
GROUP BY provider, organization_id, date_trunc('day', requested_at);

GRANT SELECT ON mcp_diag.v_cost_rollup TO arie_mcp_readonly;

-- inspect_routing_decision: the acquisition-order audit trail itself.
-- `voi_decisions` (migrations/0001) already stores rejected candidates, not
-- just chosen ones — no PII column exists on this table (all numeric/
-- enum/UUID), so it is exposed as-is.
CREATE OR REPLACE VIEW mcp_diag.v_voi_decisions AS
SELECT
    decision_id,
    lead_id,
    organization_id,
    step_number,
    candidate_provider,
    p_flips_decision,
    business_value,
    expected_cost,
    latency_penalty,
    net_evoi,
    chosen,
    confidence_before,
    confidence_after,
    created_at
FROM public.voi_decisions;

GRANT SELECT ON mcp_diag.v_voi_decisions TO arie_mcp_readonly;

-- inspect_routing_decision: a lead's own status/is_shadow context, the same
-- PII-free column set `v_job_detail` (migrations/0039) already uses for its
-- lead join — standalone here since a routing-decision lookup is by
-- lead_id, not job_id.
CREATE OR REPLACE VIEW mcp_diag.v_lead_summary AS
SELECT lead_id, status, is_shadow, organization_id, created_at
FROM public.leads;

GRANT SELECT ON mcp_diag.v_lead_summary TO arie_mcp_readonly;

-- inspect_configuration: an organization's execution_mode/status only —
-- never a plan/billing identifier. Entitlements themselves are resolved in
-- Python via `arie.billing`'s existing entitlement-resolution function
-- (spec § 7.3 reuse principle), not a second copy of that logic in SQL.
CREATE OR REPLACE VIEW mcp_diag.v_organization_summary AS
SELECT organization_id, status, execution_mode, created_at
FROM public.organizations;

GRANT SELECT ON mcp_diag.v_organization_summary TO arie_mcp_readonly;

-- inspect_configuration's entitlement resolution. `arie.billing.plans
-- .resolve_organization_entitlements(conn, organization_id=...)` reads
-- `organization_billing` directly (no grant here, same reasoning as every
-- other bare-table read this schema exists to avoid) and returns a record
-- that ALSO carries `stripe_customer_id`/`stripe_subscription_id` — fields
-- an MCP diagnostic tool must never see. This view deliberately selects
-- only the two columns `OrganizationBillingRecord.is_subscribed` and the
-- plan lookup actually depend on (`plan`, `status`) — no Stripe identifier
-- of any kind is selectable through it, at the schema layer, not by
-- promise. `arie_mcp`'s tool re-derives the same three-line branch
-- `resolve_organization_entitlements` uses, importing the real
-- `PLAN_DEFINITIONS`/`UNSUBSCRIBED`/`SUBSCRIBED_STATUSES` constants rather
-- than inventing new entitlement numbers.
CREATE OR REPLACE VIEW mcp_diag.v_organization_billing_summary AS
SELECT organization_id, plan, status
FROM public.organization_billing;

GRANT SELECT ON mcp_diag.v_organization_billing_summary TO arie_mcp_readonly;
