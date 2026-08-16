-- =============================================================================
-- 0002_metrics_views.sql — business-facing metrics
--
-- These views are deliberately NOT derived from the tracing backend. Langfuse
-- answers "why did this one LLM call behave oddly"; these views answer "what did
-- this cost us this week". Different questions, different audiences — forcing
-- one tool to serve both produces a worse answer to each.
-- =============================================================================

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
    COALESCE(pc.total_latency_ms, 0)            AS provider_latency_ms
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


-- Headline operational metrics. Cost per *qualified* lead matters more than cost
-- per lead: a system that gets cheap by rejecting everything is not an
-- improvement, and this view makes that failure mode visible rather than hidden.
CREATE OR REPLACE VIEW v_pipeline_metrics AS
SELECT
    date_trunc('day', created_at)                                   AS day,
    COUNT(*)                                                        AS leads_processed,
    AVG(total_cost_usd)                                             AS avg_cost_per_lead,
    SUM(total_cost_usd) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED','SYNCED'))
        / NULLIF(COUNT(*) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED','SYNCED')), 0)
                                                                    AS cost_per_qualified_lead,
    AVG(provider_calls)                                             AS avg_provider_calls,
    SUM(cache_hits)::NUMERIC / NULLIF(SUM(cache_hits + provider_calls), 0)
                                                                    AS cache_hit_rate,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY provider_latency_ms) AS p50_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY provider_latency_ms) AS p95_latency_ms
FROM v_lead_cost
GROUP BY 1;


CREATE OR REPLACE VIEW v_escalation_rate AS
SELECT
    date_trunc('day', l.created_at) AS day,
    COUNT(*)                                                          AS total,
    COUNT(*) FILTER (WHERE l.status = 'AWAITING_HUMAN'
                        OR hr.review_id IS NOT NULL)                  AS escalated,
    COUNT(*) FILTER (WHERE hr.final_decision IS DISTINCT FROM hr.original_decision
                       AND hr.responded_at IS NOT NULL)               AS human_overrode,
    COUNT(*) FILTER (WHERE l.status = 'AWAITING_HUMAN'
                        OR hr.review_id IS NOT NULL)::NUMERIC
        / NULLIF(COUNT(*), 0)                                         AS escalation_rate
FROM leads l
LEFT JOIN human_reviews hr ON hr.lead_id = l.lead_id
GROUP BY 1;


-- How often the cheap model was insufficient. If this trends toward 1.0 the
-- cascade is not earning its complexity and should be collapsed.
CREATE OR REPLACE VIEW v_model_escalation AS
SELECT
    date_trunc('day', created_at) AS day,
    COUNT(*) FILTER (WHERE tier = 'cheap')  AS cheap_calls,
    COUNT(*) FILTER (WHERE tier = 'strong') AS strong_calls,
    COUNT(*) FILTER (WHERE escalated_from IS NOT NULL)::NUMERIC
        / NULLIF(COUNT(*) FILTER (WHERE tier = 'cheap'), 0) AS escalation_rate
FROM model_calls
GROUP BY 1;


-- Provider health: informs both failover ordering and the simulator's realism
-- parameters once real providers are wired in.
CREATE OR REPLACE VIEW v_provider_health AS
SELECT
    provider,
    COUNT(*)                                                     AS total_calls,
    COUNT(*) FILTER (WHERE status = 'success')::NUMERIC
        / NULLIF(COUNT(*), 0)                                    AS success_rate,
    COUNT(*) FILTER (WHERE status = 'miss')::NUMERIC
        / NULLIF(COUNT(*), 0)                                    AS miss_rate,
    COUNT(*) FILTER (WHERE status IN ('error','timeout'))::NUMERIC
        / NULLIF(COUNT(*), 0)                                    AS failure_rate,
    AVG(cost_usd)                                                AS avg_cost_usd,
    PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms)     AS p50_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)     AS p95_latency_ms
FROM provider_calls
WHERE NOT cache_hit
GROUP BY provider;
