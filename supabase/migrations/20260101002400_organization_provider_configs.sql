-- =============================================================================
-- 0025_organization_provider_configs.sql — Productization M4 Parts 3-7:
-- organization-owned BYOK credentials for the three EXISTING live provider
-- adapters (`arie.live.providers.REGISTERED_LIVE_PROVIDER_NAMES` — Abstract,
-- Hunter, Apollo; the CHECK constraint below must be kept in sync with that
-- tuple, the same "mirrored in SQL and Python, kept in sync by convention"
-- pattern `arie.auth.ROLES` already uses for `organization_members.role`).
-- No new provider is introduced here or anywhere in M4.
--
-- **Never a raw secret in this table.** `vault_secret_id` is a foreign key
-- in spirit only (no DB-level `REFERENCES` — `vault.secrets` is a
-- Supabase-managed schema this migration does not own or want to couple a
-- constraint to) pointing at a row in Supabase Vault
-- (`supabase_vault`/`pgsodium`, confirmed enabled in production — see
-- `arie.vault`'s own docstring for the verified privilege boundary: only
-- `postgres`/`service_role` can ever read a decrypted secret; `authenticated`
-- /`anon` have zero grants on `vault.secrets`/`vault.decrypted_secrets`).
-- This table stores only metadata a browser is safe to see the *shape* of —
-- `enabled`, `last_tested_at`, `last_test_status`, a sanitized
-- `last_test_error` — never a credential value in any form, encrypted or not.
--
-- **Vault and this table's writes share one real Postgres transaction** —
-- not merely "as close to atomic as practical." Vault is implemented as
-- ordinary tables/functions in the *same* Postgres database this migration
-- runs against, not an external secret-management service, so
-- `arie.provider_configs.set_provider_credential`'s `vault.create_secret`/
-- `vault.update_secret` call and this table's own INSERT/UPDATE genuinely
-- commit or roll back together. A credential row can never be orphaned
-- pointing at a Vault secret that was never actually committed, or vice
-- versa.
--
-- **Reads open to any active member, writes owner/admin-only** — the same
-- split `organization_icp_profiles` uses, per the brief's own "active
-- member can probably read safe metadata" (writes are the security-
-- sensitive action; the response shape below never contains a secret, so
-- there is nothing an ordinary member reading it could leak). Unlike ICP
-- profiles, rows here are genuinely deletable (`DELETE /organization
-- /providers/{provider}` — "remove credential" is a real, requested
-- capability): there is no permanent-audit-history reason to keep a removed
-- credential's metadata row around once its Vault secret is gone too. The
-- historical *fact* that a credential was removed still survives, in
-- `organization_audit_events`.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_provider_configs (
    config_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    provider           TEXT NOT NULL
                            CHECK (provider IN (
                                'abstract_company_enrichment',
                                'hunter_combined_enrichment',
                                'apollo_person_enrichment'
                            )),
    enabled            BOOLEAN NOT NULL DEFAULT true,
    vault_secret_id    UUID NOT NULL,
    created_by_user_id UUID NOT NULL,
    updated_by_user_id UUID NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_tested_at     TIMESTAMPTZ,
    last_test_status   TEXT CHECK (last_test_status IN ('success', 'failure')),
    last_test_error    TEXT,
    CONSTRAINT organization_provider_configs_org_provider_unique UNIQUE (organization_id, provider)
);

-- The provider-list query (GET /organization/providers).
CREATE INDEX IF NOT EXISTS idx_organization_provider_configs_org
    ON organization_provider_configs(organization_id);

ALTER TABLE organization_provider_configs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_provider_configs_select ON organization_provider_configs;
CREATE POLICY organization_provider_configs_select ON organization_provider_configs FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS organization_provider_configs_admin_insert ON organization_provider_configs;
CREATE POLICY organization_provider_configs_admin_insert ON organization_provider_configs FOR INSERT
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

DROP POLICY IF EXISTS organization_provider_configs_admin_update ON organization_provider_configs;
CREATE POLICY organization_provider_configs_admin_update ON organization_provider_configs FOR UPDATE
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

DROP POLICY IF EXISTS organization_provider_configs_admin_delete ON organization_provider_configs;
CREATE POLICY organization_provider_configs_admin_delete ON organization_provider_configs FOR DELETE
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']));
