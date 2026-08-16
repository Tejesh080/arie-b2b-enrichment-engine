-- =============================================================================
-- 0005_ingestion_ledger_tracing.sql — M1 Step 9 (ingestion API, cost ledger, OTel)
--
-- Everything here is additive or a view replacement; no column is dropped and no
-- existing row is rewritten. Like every migration in this repo it must stay
-- re-runnable against a database that already has it (ADR 0005): a Supabase
-- preview branch applies these from scratch, and Branching's production deploy
-- would too if it were ever enabled.
-- =============================================================================

-- ------------------------------------------------------- trace propagation --

-- The W3C trace context captured when the job was enqueued, so a worker that
-- claims it minutes later continues the *same* trace as the HTTP request that
-- created it instead of starting an orphan one.
--
-- The queue row is the only place that link can survive. The API process and
-- the worker process share no memory and no call stack; the job row is the one
-- artefact that crosses the boundary between them, so it is where the causal
-- edge has to be written down. Stored as the propagator's own carrier map
-- (`traceparent`, and `tracestate` when a vendor set one) rather than a parsed
-- trace/span id pair — the carrier is the format the OTel propagator both emits
-- and consumes, so round-tripping it can't drift from the spec.
--
-- Nullable on purpose: a job enqueued outside a trace (a maintenance job, a
-- manual backfill, anything run before tracing was configured) is still a
-- perfectly valid job. `arie.observability.tracing.extract_trace_context`
-- treats NULL as "start a new trace", never as an error.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS trace_context JSONB;

-- ------------------------------------------------------ ledger idempotency --

-- `provider_calls` has carried a UNIQUE idempotency_key since 0001_init.sql —
-- that column is what makes "never double-charge a provider on retry"
-- (ADR 0002) enforceable by the database rather than by convention.
-- `model_calls` was created without one, so a retried LLM call would have been
-- billed twice in the ledger and every cost view would have silently
-- overcounted. Added now, before Step 10 wires a real model and makes the gap
-- expensive.
--
-- Nullable, unlike provider_calls' NOT NULL: model_calls already exists and may
-- hold rows, so a NOT NULL column can't be added without inventing keys for
-- them. NULL never conflicts under a UNIQUE index, which gives exactly the
-- semantics `jobs.idempotency_key` already documents — a caller with no natural
-- dedup key simply always gets a new row.
ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_calls_idempotency
    ON model_calls(idempotency_key);

-- v_lead_cost aggregates model_calls by lead_id and had no index for it;
-- provider_calls has had idx_provider_calls_lead since 0001.
CREATE INDEX IF NOT EXISTS idx_model_calls_lead ON model_calls(lead_id);

-- ------------------------------------------------------ stale budget default --

-- DEFECT — `leads.budget_usd_cap` defaulted to a value the policy config had
-- already rejected as broken.
--
-- 0001_init.sql set `DEFAULT 0.50`. `arie.config.PolicyConfig.lead_budget_usd_cap`
-- carries a long comment explaining why that exact figure was raised to 1.50:
-- the per-lead cap must sit *above* the cost of calling every provider once, and
-- $0.50 is below `deep_research`'s $0.600 list price — the only source of the
-- disqualifying flag. A lead capped at $0.50 can therefore never buy the one
-- provider that detects a blocker, whatever the policy decides. The fix landed
-- in the config and never reached the column.
--
-- Ingestion now always writes the cap explicitly from `PolicyConfig`
-- (`arie.api.ingest`), so this default no longer applies on the request path.
-- It is corrected anyway so the trap isn't left lying in wait for a direct
-- INSERT — a backfill, a fixture, an n8n workflow writing rows itself.
-- Existing rows are untouched: a DEFAULT change never rewrites a table.
ALTER TABLE leads ALTER COLUMN budget_usd_cap SET DEFAULT 1.50;

-- ------------------------------------------------- corrections to 0002's views --
--
-- Both of the below are genuine defects, found by writing the first tests that
-- actually execute these views against data (tests/integration/
-- test_cost_ledger_integration.py). They were never wrong in an obvious way —
-- they returned plausible numbers, which is precisely why they survived until
-- something checked them arithmetically.

-- DEFECT 1 — `cost_per_qualified_lead` counted rejected leads as qualified.
--
-- The 'SYNCED' status is where the *reject* branch terminates:
-- `arie.statemachine.transitions.DECISION_OUTCOMES` maps
-- "reject" -> LeadStatus.SYNCED ("nothing further to route"). Including SYNCED
-- in the qualified set therefore counted every rejection as a qualified lead —
-- and the more leads the system rejected, the better cost-per-qualified-lead
-- would look. That is the exact failure mode 0002's own comment says this view
-- exists to make visible ("a system that gets cheap by rejecting everything is
-- not an improvement"), inverted.
--
-- Qualified is now AUTO_ROUTED and ROUTED only: the two statuses that mean a
-- lead was actually passed on to a human or a CRM.
--
-- Known follow-up, stated rather than silently absorbed: SYNCED is ambiguous by
-- construction — it is today only reachable from reject, but a future ROUTED ->
-- SYNCED CRM sync step (M1 step 12, n8n edge workflows) would make it reachable
-- from the qualified path too, and this filter would be wrong again in the other
-- direction. The durable fix is a distinct terminal status for rejection rather
-- than reusing SYNCED, which is a state-graph change and belongs with the step
-- that adds the sync path, not with this one.
CREATE OR REPLACE VIEW v_pipeline_metrics AS
SELECT
    date_trunc('day', created_at)                                   AS day,
    COUNT(*)                                                        AS leads_processed,
    AVG(total_cost_usd)                                             AS avg_cost_per_lead,
    SUM(total_cost_usd) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED'))
        / NULLIF(COUNT(*) FILTER (WHERE status IN ('AUTO_ROUTED','ROUTED')), 0)
                                                                    AS cost_per_qualified_lead,
    AVG(provider_calls)                                             AS avg_provider_calls,
    SUM(cache_hits)::NUMERIC / NULLIF(SUM(cache_hits + provider_calls), 0)
                                                                    AS cache_hit_rate,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY provider_latency_ms) AS p50_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY provider_latency_ms) AS p95_latency_ms
FROM v_lead_cost
GROUP BY 1;


-- DEFECT 2 — a lead with N human reviews was counted N times.
--
-- `LEFT JOIN human_reviews` fans a lead out to one row per review, so COUNT(*)
-- counted (lead, review) pairs rather than leads. `total` was inflated for any
-- lead reviewed more than once — which is exactly the lead a re-review or a
-- disputed decision produces — and `escalation_rate` was wrong in both
-- directions at once: the same lead inflated the numerator and the denominator
-- unequally, since a non-escalated lead contributes exactly one row and an
-- N-times-reviewed lead contributes N.
--
-- Counting distinct leads fixes the rate. `human_overrode` stays per-review
-- (counted distinctly by review_id) because an override genuinely is an event
-- per review, not per lead — two reviewers overriding the same lead is two
-- overrides.
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
GROUP BY 1;
