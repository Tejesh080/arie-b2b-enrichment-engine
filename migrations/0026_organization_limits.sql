-- =============================================================================
-- 0026_organization_limits.sql — Productization M4 Part 9: sensible,
-- server-enforced usage ceilings. Not billing — no plan tiers, no payment
-- integration, nothing beyond "the configured number" (see
-- `arie.limits`'s own docstring for the full non-goal statement).
--
-- Structured columns directly on `organizations`, not a separate
-- `organization_limits` table — the same choice `migrations/0023_organization
-- _settings.sql` made for timezone/company_domain/onboarding, and the M4
-- brief's own suggested option ("structured columns/config on an
-- organization plan/settings table") given there is no `organization_plans`
-- table this early to hang a separate limits table off of.
--
-- Every column has a real, non-NULL default so every existing organization
-- (this migration adds no backfill step — `ADD COLUMN ... NOT NULL DEFAULT`
-- applies the default to every existing row atomically) is immediately
-- covered by a sensible ceiling rather than reading as "unlimited" until
-- someone remembers to configure it.
--
-- `max_csv_rows_per_upload` defaults to exactly `arie.batches.MAX_ROWS`
-- (200) — the existing *technical* hard cap this migration does not
-- change or duplicate. An organization's own value is enforced as an
-- additional, tighter-or-equal business ceiling on top of that technical
-- one, never a way to exceed it — see `arie.limits.enforce_csv_row_quota`.
-- =============================================================================

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS max_leads_per_month INT NOT NULL DEFAULT 5000
        CHECK (max_leads_per_month >= 0),
    ADD COLUMN IF NOT EXISTS max_csv_rows_per_upload INT NOT NULL DEFAULT 200
        CHECK (max_csv_rows_per_upload >= 0),
    ADD COLUMN IF NOT EXISTS max_modeled_spend_usd_per_month NUMERIC(12, 4) NOT NULL DEFAULT 50.0000
        CHECK (max_modeled_spend_usd_per_month >= 0);
