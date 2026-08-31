-- =============================================================================
-- 0028_provider_call_cost_and_credential_provenance.sql — Productization M5
-- Parts 1 and 5: two nullable, additive columns on `provider_calls`.
--
-- `credential_source` — which credential a real (non-simulated) call used:
-- 'organization' for an organization's own Vault-stored BYOK credential
-- (`arie.credential_resolver.resolve_provider_credential`, wired into
-- acquisition by `arie.live.provider_availability` in this milestone), or
-- 'system' for the process-wide env-var credential
-- (`arie.config.LIVE_PROVIDER`/`HUNTER`/`APOLLO_PERSON`), which after this
-- milestone is reachable only through explicit test/smoke-script injection
-- (`live_provider`/`live_providers` in `arie.jobs.handlers.build_handlers`) —
-- never through ordinary tenant job processing. NULL for every row written
-- before this migration, and for every simulated-mode row going forward
-- (the column does not apply — nothing was borrowed from any vault).
--
-- `actual_cost_usd` — the provider's own billed figure for this call, only
-- when the vendor's response explicitly states one. `cost_usd` (0001) stays
-- exactly what it always was: ARIE's *modeled* cost, from
-- `arie.config`'s per-provider `cost_usd_per_call`/`cost_usd_per_success`
-- constants, used for every acquisition-policy and spend-cap decision before
-- and after this migration. None of Abstract, Hunter, or Apollo's
-- documented API responses (verified against their published docs, not
-- inferred) return a per-call billed-dollar figure — their `raw` payloads
-- carry credits/plan-metering information at best, which is exactly what
-- `credits_used`/`cost_basis` (0010) already capture. So `actual_cost_usd`
-- is expected to be NULL for every call from all three of today's adapters;
-- the column exists so a future provider that *does* report billed cost has
-- somewhere honest to put it, rather than that provider's adapter forcing a
-- new migration or, worse, being tempted to write its billed figure into
-- `cost_usd` and quietly change what every existing cap/report means.
-- Nothing in this codebase may ever compute or estimate a value for this
-- column — only a value the vendor's own response explicitly stated.
-- =============================================================================

ALTER TABLE provider_calls
    ADD COLUMN IF NOT EXISTS credential_source TEXT,
    ADD COLUMN IF NOT EXISTS actual_cost_usd NUMERIC;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'provider_calls_credential_source_check'
    ) THEN
        ALTER TABLE provider_calls
            ADD CONSTRAINT provider_calls_credential_source_check
            CHECK (credential_source IS NULL OR credential_source IN ('organization', 'system'));
    END IF;
END $$;
