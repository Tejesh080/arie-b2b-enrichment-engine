-- =============================================================================
-- 0009_live_provider_and_shadow_mode.sql — post-M1 P5 (one real provider + shadow mode)
--
-- Additive-only: one new column, two view replacements (both strictly adding a
-- filter, nothing removed or retyped). Must stay re-runnable against a database
-- that already has it (ADR 0005).
--
-- Why a column on `leads` rather than a new table: shadow-ness has to be known
-- from the moment a lead is ingested (before any decision exists), it never
-- changes after creation (see arie.api.ingest's idempotency semantics — the
-- same "first write wins" rule every other optional ingestion field already
-- follows), and it has to be visible to `v_pipeline_metrics`/`v_escalation_rate`
-- so a shadow evaluation's cost and escalation activity never quietly inflates
-- those business-facing metrics. `decision_receipts` (0008) is keyed by lead
-- and already 1:1 with leads.lead_id; reading `leads.is_shadow` at receipt time
-- is simpler than duplicating the flag onto a second table.
-- =============================================================================

ALTER TABLE leads ADD COLUMN IF NOT EXISTS is_shadow BOOLEAN NOT NULL DEFAULT false;

-- v_lead_cost stays universal (every lead, shadow or not) — GET /leads/{id}/receipt
-- and GET /leads/{id} both read a single lead's own cost row through it regardless
-- of shadow status, so filtering here would break a shadow lead's own receipt.
-- `is_shadow` is added as a plain passthrough column so the two *aggregate*
-- business views below can filter on it without re-joining `leads` themselves.
-- Appended as the LAST column, not inserted after `status` — `CREATE OR
-- REPLACE VIEW` can only add columns at the end; Postgres treats inserting
-- one in the middle as renaming every column after it, which it refuses
-- ("cannot change name of view column ... to ...").
CREATE OR REPLACE VIEW v_lead_cost AS
SELECT
    l.lead_id,
    l.status,
    l.created_at,
    COALESCE(pc.provider_cost, 0)               AS provider_cost_usd,
    COALESCE(mc.model_cost, 0)                  AS model_cost_usd,
    COALESCE(pc.provider_cost, 0) + COALESCE(mc.model_cost, 0) AS total_cost_usd,
    COALESCE(pc.calls_made, 0)                  AS provider_calls,
    COALESCE(pc.cache_hits, 0)                  AS cache_hits,
    COALESCE(pc.total_latency_ms, 0)            AS provider_latency_ms,
    l.is_shadow
FROM leads l
LEFT JOIN (
    SELECT lead_id,
           SUM(cost_usd)                              AS provider_cost,
           COUNT(*) FILTER (WHERE NOT cache_hit)      AS calls_made,
           COUNT(*) FILTER (WHERE cache_hit)          AS cache_hits,
           SUM(latency_ms)                            AS total_latency_ms
    FROM provider_calls GROUP BY lead_id
) pc ON pc.lead_id = l.lead_id
LEFT JOIN (
    SELECT lead_id, SUM(cost_usd) AS model_cost
    FROM model_calls GROUP BY lead_id
) mc ON mc.lead_id = l.lead_id;

-- DEFECT (introduced by P5, fixed in the same migration that introduces it) —
-- without this filter, a shadow evaluation's spend and provider-call activity
-- would silently count toward `leads_processed`/`avg_cost_per_lead`/
-- `cache_hit_rate`, exactly the "shadow-mode superiority" or "production cost"
-- conflation the P5 brief explicitly rules out. Same logic (`SUM(...) FILTER
-- (WHERE status IN (...))`) as 0007, just scoped to non-shadow rows first.
CREATE OR REPLACE VIEW v_pipeline_metrics AS
SELECT
    date_trunc('day', created_at)                                   AS day,
    COUNT(*)                                                        AS leads_processed,
    AVG(total_cost_usd)                                             AS avg_cost_per_lead,
    SUM(total_cost_usd) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED','MANUAL_REVIEW'))
        / NULLIF(COUNT(*) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED','MANUAL_REVIEW')), 0)
                                                                    AS cost_per_qualified_lead,
    AVG(provider_calls)                                             AS avg_provider_calls,
    SUM(cache_hits)::NUMERIC / NULLIF(SUM(cache_hits + provider_calls), 0)
                                                                    AS cache_hit_rate,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY provider_latency_ms) AS p50_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY provider_latency_ms) AS p95_latency_ms
FROM v_lead_cost
WHERE NOT is_shadow
GROUP BY 1;

-- Same reasoning as v_pipeline_metrics above: a shadow lead that would have
-- escalated must not move the real escalation rate, and a shadow evaluation
-- never opens a real `human_reviews` row anyway (arie.jobs.handlers never
-- calls request_review for a shadow lead), so this filter only ever removes
-- shadow leads' contribution to the `total`/denominator side.
CREATE OR REPLACE VIEW v_escalation_rate AS
SELECT
    date_trunc('day', l.created_at) AS day,
    COUNT(DISTINCT l.lead_id)                                         AS total,
    COUNT(DISTINCT l.lead_id) FILTER (WHERE l.status = 'AWAITING_HUMAN'
                                         OR hr.review_id IS NOT NULL) AS escalated,
    COUNT(DISTINCT hr.review_id) FILTER (
        WHERE hr.final_decision IS DISTINCT FROM hr.original_decision
          AND hr.responded_at IS NOT NULL)                            AS human_overrode,
    COUNT(DISTINCT l.lead_id) FILTER (WHERE l.status = 'AWAITING_HUMAN'
                                         OR hr.review_id IS NOT NULL)::NUMERIC
        / NULLIF(COUNT(DISTINCT l.lead_id), 0)                        AS escalation_rate
FROM leads l
LEFT JOIN human_reviews hr ON hr.lead_id = l.lead_id
WHERE NOT l.is_shadow
GROUP BY 1;
