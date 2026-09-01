-- =============================================================================
-- 0029_provider_call_uncertain_outcome_suppression.sql — Productization M5
-- Issue 3 (retry safety): a third `suppressed_reason` value, additive to
-- migration 0011's `recent_miss`/`recent_partial`.
--
-- The gap this closes. `arie.live.outcome_cache.ProviderOutcomeGuard`
-- already stops a worker retry from re-buying a *settled* miss or partial
-- success. It had nothing to say about a genuinely *uncertain* outcome — a
-- timeout, or a connection-level transport failure — where ARIE cannot tell
-- whether the vendor received (and, for a provider that bills on lookup
-- rather than on match, may have already started billing-relevant
-- processing of) the request before the connection dropped. Without this,
-- a job that crashes or is retried after such a call re-issues the exact
-- same paid request, blind to whether the first one may have already been
-- charged.
--
-- `'uncertain_outcome'` marks a zero-cost, zero-latency ledger row recorded
-- when `arie.live.outcome_cache.ProviderOutcomeGuard.recent_uncertain_
-- outcome` finds a recent settled `error_kind IN ('timeout') OR error_kind
-- LIKE 'transport_error:%'` row for the same organization+provider+entity
-- and suppresses a repeat attempt — the same shape migration 0011 already
-- established for `recent_miss`/`recent_partial`, extended rather than
-- duplicated. NULL for every row written before this migration, for every
-- real call, and for an ordinary evidence-cache hit — unchanged.
-- =============================================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'provider_calls_suppressed_reason_check'
    ) THEN
        ALTER TABLE provider_calls DROP CONSTRAINT provider_calls_suppressed_reason_check;
    END IF;
    ALTER TABLE provider_calls
        ADD CONSTRAINT provider_calls_suppressed_reason_check
        CHECK (suppressed_reason IS NULL
               OR suppressed_reason IN ('recent_miss', 'recent_partial', 'uncertain_outcome'));
END $$;
