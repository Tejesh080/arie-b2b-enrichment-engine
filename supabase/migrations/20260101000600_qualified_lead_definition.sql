-- =============================================================================
-- 0007_qualified_lead_definition.sql — post-M1 audit freeze fix
--
-- Additive-only: replaces one view definition, drops and adds nothing. Must
-- stay re-runnable against a database that already has it (ADR 0005).
-- =============================================================================

-- DEFECT — `cost_per_qualified_lead` filtered on a status nothing ever writes
-- and excluded one a human-reviewed lead actually reaches.
--
-- 0005 fixed this view once already (its own DEFECT 1 comment above the
-- previous definition): SYNCED is the *reject* branch's terminal
-- (arie.statemachine.transitions.DECISION_OUTCOMES / HUMAN_REVIEW_OUTCOMES
-- both map "reject" -> SYNCED, "nothing further to route"), so counting it as
-- qualified rewards rejecting more leads. That fix narrowed the filter to
-- `('AUTO_ROUTED','ROUTED')` — but 0005 predates M1 Step 11's human review
-- workflow, and missed two things once that step existed:
--
--   1. `ROUTED` is not reachable by anything in this codebase. Every status
--      the state graph can actually produce is enumerated in
--      arie.statemachine.transitions.DECISION_OUTCOMES and
--      HUMAN_REVIEW_OUTCOMES; ROUTED appears in neither. It is reserved for
--      a future ROUTED-sync step (see 0005's own "known follow-up" comment)
--      and today is dead weight in the filter, not wrong, just inert.
--   2. `MANUAL_REVIEW` — the terminal a human's `action=edit` decision
--      reaches (HUMAN_REVIEW_OUTCOMES: "manual_review" -> LeadStatus.
--      MANUAL_REVIEW, arie.approval.workflow._FINAL_DECISION) — is a
--      genuinely qualified outcome (a human materially routed the lead) and
--      was excluded from both the numerator and the denominator. Every
--      human-edited lead's spend landed in `leads_processed` but never in
--      `cost_per_qualified_lead`'s count, silently inflating the metric
--      exactly as more of the human-review path got used — the same
--      direction of error 0005 fixed once for the automatic-reject path,
--      one branch over.
--
-- `arie.statemachine.transitions.QUALIFIED` is now the one place this
-- vocabulary is defined (AUTO_ROUTED, ROUTED, MANUAL_REVIEW) — this view
-- restates it in SQL rather than inventing a second definition;
-- `tests/integration/test_cost_ledger_integration.py` pins the two against
-- each other so they can't drift apart the way 0002's and 0005's definitions
-- did.
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
GROUP BY 1;
