-- =============================================================================
-- 0023_organization_settings.sql — Productization M4 Part 1: organization
-- display/settings fields, and the first UPDATE policy `organizations` has
-- ever had.
--
-- `organizations` already carries `name` (display name), `slug`,
-- `created_at`/`updated_at` since `migrations/0012_organizations_and_members
-- .sql` — this migration adds only what is genuinely missing: `timezone`
-- (an IANA name; validated at the application layer, not a DB CHECK, so a
-- new zone name never needs a migration to become acceptable),
-- `company_domain` (optional — an organization is not required to have one),
-- and `onboarding_completed_at`.
--
-- `onboarding_completed_at` follows this schema's existing nullable-timestamp
-- -as-status idiom (`organization_api_keys.revoked_at`,
-- `organization_icp_profiles.retired_at`) rather than a boolean: NULL means
-- "still onboarding", non-NULL records both the fact and exactly when. The
-- per-step onboarding checklist (account/org/ICP/providers/first-upload) is
-- deliberately NOT stored here or anywhere else — every step it would track
-- is already derivable from existing tables (an active ICP profile version,
-- a row in `lead_batches`, and so on), so storing it again would just be a
-- second copy that could drift from the tables that are already the source
-- of truth. This one column exists only for the coarse "has this
-- organization ever finished setup" fact a returning user's UI needs
-- immediately, before any of those per-step queries run.
--
-- `organizations` has had RLS enabled with a SELECT-only policy since
-- `migrations/0016_row_level_security.sql` — no UPDATE policy has existed
-- until now because there was nothing on this table an organization member
-- could legitimately change. `PATCH /organization` is the first such action;
-- the policy below is defense-in-depth exactly like every other one in this
-- schema (the API's own pooled connection is service-role and bypasses RLS;
-- `arie.api.main`'s `_require_org_admin` gate is the real enforcement) —
-- see `migrations/0016_row_level_security.sql`'s own docstring.
-- =============================================================================

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'UTC',
    ADD COLUMN IF NOT EXISTS company_domain TEXT,
    ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMPTZ;

DROP POLICY IF EXISTS organizations_admin_update ON organizations;
CREATE POLICY organizations_admin_update ON organizations FOR UPDATE
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));
