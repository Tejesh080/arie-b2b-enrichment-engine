-- =============================================================================
-- 0010_provider_call_cost_provenance.sql — multi-provider cost provenance
--
-- Additive-only: three nullable columns on `provider_calls`, one partial
-- index. Must stay re-runnable against a database that already has it
-- (ADR 0005).
--
-- Why now. Until this step every live row's `cost_usd` was one vendor's one
-- pricing model, and `status = 'error'` was one adapter's small set of
-- failure modes — the shape of the column set matched a single-provider
-- world. With three vendors metering three different ways (Abstract bills
-- every lookup in dollars-modelled-from-a-plan; Apollo bills 1 credit per
-- match; Hunter bills 0.2 credits per successful enrichment), a `cost_usd`
-- figure alone can no longer be audited back to what the vendor actually
-- counted, and a bare `'error'` can no longer tell "this vendor is down"
-- apart from "this account is out of credits" — which the quota-cooldown
-- guard needs to read durably, across workers, from the same ledger the
-- spend caps already read.
--
-- `error_kind`   — the adapter's stable failure vocabulary
--                  (`authentication_failed`, `rate_limited`, `quota_exhausted`,
--                  `insufficient_credits`, `server_error`, `timeout`,
--                  `transport_error:*`, ...). NULL for success/miss rows and
--                  for every row written before this migration; the cooldown
--                  guard treats NULL as "not a quota failure", which is the
--                  safe reading of an old row.
-- `credits_used` — the VENDOR'S OWN metering unit for this call, when the
--                  vendor meters in credits (Apollo: 1 per match; Hunter: 0.2
--                  per successful enrichment). NULL where the vendor prices in
--                  currency (Abstract) or nothing was consumed. NUMERIC, not
--                  INT: Hunter's unit is fractional.
-- `cost_basis`   — what `cost_usd` on this row actually IS:
--                  'modelled_credit_equivalent' (credits × a configured rate),
--                  'modelled_list_price' (a plan's list price ÷ volume), or —
--                  reserved, nothing writes it yet — 'vendor_billed' for a
--                  figure literally taken from an invoice. NULL on old rows
--                  and cache hits. This is the column that keeps the Decision
--                  Receipt honest: "configured/modelled acquisition cost" and
--                  "actual vendor invoice" are different claims, and a ledger
--                  that can't say which one a number is will eventually be
--                  read as making the stronger one.
-- =============================================================================

ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS error_kind   TEXT;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS credits_used NUMERIC;
ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS cost_basis   TEXT;

-- The quota-cooldown guard's exact lookup: "has provider X hit a quota wall
-- recently?". Partial on the interesting rows so the index stays tiny — the
-- overwhelming majority of ledger rows are successes with error_kind IS NULL.
CREATE INDEX IF NOT EXISTS idx_provider_calls_quota
    ON provider_calls(provider, completed_at DESC)
    WHERE error_kind IN ('quota_exhausted', 'insufficient_credits');
