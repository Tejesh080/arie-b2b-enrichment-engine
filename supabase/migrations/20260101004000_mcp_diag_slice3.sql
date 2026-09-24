-- =============================================================================
-- 0041_mcp_diag_slice3.sql — MCP Engineering Interface slice 3, adding exactly
-- what two new bounded discovery tools need: `list_organizations` and
-- `list_recent_leads`. Both exist to close one specific, confirmed gap: an
-- MCP caller had no way to discover a lead_id or organization_id to feed
-- inspect_routing_decision/inspect_provider_health/get_enrichment_costs
-- without already having one from outside MCP.
--
-- Same security shape as 0039/0040: one explicit GRANT SELECT per view, no
-- ALTER DEFAULT PRIVILEGES, no base-table grant. Deliberately excludes
-- `organizations.name` (the customer's own business/display name) in favour
-- of `slug` — still a legible label for an operator, but the more
-- conservative of the two given this schema's job is engineering diagnostics,
-- not a CRM view. Excludes every `organization_members`/`persons`/
-- `companies` column — no member or contact PII is reachable through either
-- view under any join. `decision_receipts.evidence_snapshot` (free-text
-- field-resolution detail) is deliberately NOT selected here — only the
-- scalar decision/score/confidence/stop_reason columns, which the existing
-- customer-facing GET /leads/{lead_id}/receipt already exposes.
--
-- Must stay re-runnable against a database that already has it (ADR 0005).
-- =============================================================================

-- list_organizations: tenant summary with a lightweight activity rollup, so
-- an operator (or this MCP server) can discover which organizations exist
-- and which of them have ever generated real activity, without a base-table
-- grant on `organizations`/`leads`.
CREATE OR REPLACE VIEW mcp_diag.v_organization_activity AS
SELECT
    o.organization_id,
    o.slug,
    o.status,
    o.execution_mode,
    o.created_at,
    ob.plan                    AS billing_plan,
    la.lead_count,
    la.last_lead_created_at
FROM public.organizations o
LEFT JOIN public.organization_billing ob ON ob.organization_id = o.organization_id
LEFT JOIN LATERAL (
    SELECT count(*) AS lead_count, max(l.created_at) AS last_lead_created_at
    FROM public.leads l
    WHERE l.organization_id = o.organization_id
) la ON true;

GRANT SELECT ON mcp_diag.v_organization_activity TO arie_mcp_readonly;

-- list_recent_leads: the same PII-free lead identity columns v_lead_summary
-- (migrations/0040) already exposes, plus `updated_at`, plus — only the
-- scalar columns — its decision_receipt if one exists (score/bounds/
-- confidence/decision/stop_reason: spec's "score/bounds/evidence_sufficiency
-- where available" requirement; the derived evidence_sufficiency verdict
-- itself needs per-organization ICP thresholds this view does not resolve,
-- so it is intentionally left to the caller to derive or omit rather than
-- re-implementing `arie.recommendations`'s threshold logic a second time in
-- SQL), plus its most recent job's id/status (job linkage, so a caller can
-- chain into `inspect_job` without a second discovery round-trip).
CREATE OR REPLACE VIEW mcp_diag.v_lead_recent AS
SELECT
    l.lead_id,
    l.organization_id,
    l.status,
    l.is_shadow,
    l.created_at,
    l.updated_at,
    dr.decision       AS receipt_decision,
    dr.autonomous      AS receipt_autonomous,
    dr.confidence       AS receipt_confidence,
    dr.score_value        AS receipt_score_value,
    dr.score_lower          AS receipt_score_lower,
    dr.score_upper            AS receipt_score_upper,
    dr.stop_reason              AS receipt_stop_reason,
    lj.job_id                      AS latest_job_id,
    lj.job_status                     AS latest_job_status
FROM public.leads l
LEFT JOIN public.decision_receipts dr ON dr.lead_id = l.lead_id
LEFT JOIN LATERAL (
    SELECT j.job_id, j.status AS job_status
    FROM public.jobs j
    WHERE j.lead_id = l.lead_id
    ORDER BY j.created_at DESC
    LIMIT 1
) lj ON true;

GRANT SELECT ON mcp_diag.v_lead_recent TO arie_mcp_readonly;
