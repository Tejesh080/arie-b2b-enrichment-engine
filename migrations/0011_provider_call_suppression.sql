-- =============================================================================
-- 0011_provider_call_suppression.sql — truthful "we didn't ask" rows
--
-- Additive-only: one nullable column on `provider_calls`, one CHECK
-- constraint. Must stay re-runnable against a database that already has it
-- (ADR 0005).
--
-- Why now. The 2026-08-29 abstract-hunter-live-1 experiment found that a
-- provider whose earlier answer left one of its declared fields genuinely
-- unmapped (a "partial" success) or that found nothing at all (a MISS) got
-- re-asked on the very next identical request — the evidence-freshness cache
-- only recognises "every declared field is already held," so a partial or a
-- miss never counted as "already answered." `arie.live.outcome_cache` closes
-- that gap by treating a recent settled outcome (success-with-some-fields,
-- or miss) as reason enough to skip a re-call — but skipping still needs a
-- ledger row (the call slot in the acquisition sequence is accounted for,
-- and `v_lead_cost`'s zero-cost `cache_hit` semantics already cover the
-- "didn't pay" half of the story).
--
-- What this column adds is the truthful *why*: without it, a suppressed call
-- would have to reuse `cache_hit = true` with nothing distinguishing it from
-- an evidence-cache hit that legitimately reused a field's *value* — exactly
-- the "fabricated cache_hit evidence row" this migration exists to avoid.
--
-- `suppressed_reason` — NULL for every real call (whether it succeeded,
--                        missed, errored, or timed out) and for a plain
--                        evidence-cache hit (a field's value, reused).
--                        'recent_miss' when the skip was because this exact
--                        provider+entity settled on MISS inside its TTL.
--                        'recent_partial' when the skip was because this
--                        provider already holds *some* but not all of its
--                        declared fields, still fresh. NULL on every row
--                        written before this migration — the safe reading of
--                        an old row is "this was a real call or an ordinary
--                        cache hit," never a suppression this column didn't
--                        exist to record.
-- =============================================================================

ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS suppressed_reason TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'provider_calls_suppressed_reason_check'
    ) THEN
        ALTER TABLE provider_calls
            ADD CONSTRAINT provider_calls_suppressed_reason_check
            CHECK (suppressed_reason IS NULL OR suppressed_reason IN ('recent_miss', 'recent_partial'));
    END IF;
END $$;
