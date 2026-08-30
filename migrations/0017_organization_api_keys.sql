-- =============================================================================
-- 0017_organization_api_keys.sql — Productization M2A: machine-to-machine
-- authentication for n8n, scripts, CRM/webhooks, and future integrations,
-- without requiring a Supabase user session.
--
-- Only `key_hash` (a SHA-256 digest) and `key_prefix` (the first 12 characters
-- of the raw key, including the `arie_` tag) are ever persisted — the raw key
-- itself is generated, hashed, and handed to the caller once
-- (`arie.apikeys.create_api_key`) and is never written to this table, any
-- other table, or a log line. `key_prefix` is what makes the verification
-- query on every authenticated request an indexed point lookup rather than a
-- table scan comparing hashes; it is not a secret, and showing it back to an
-- admin in the key-list endpoint is what lets them tell two keys apart
-- without ever seeing the raw value again.
--
-- `revoked_at IS NULL` means active — the same "nullable timestamp as status"
-- idiom `human_reviews.responded_at` already uses in this schema, chosen for
-- the same reason: a revocation is a one-time, irreversible event, not a
-- toggle, so a timestamp both records *whether* and *when* in one column.
--
-- `created_by_user_id` carries no foreign key to `auth.users` — see
-- `organization_members.user_id`'s identical note in
-- `migrations/0012_organizations_and_members.sql` for why (that schema does
-- not exist on a plain local/CI Postgres, and migrations must stay
-- re-runnable against both, ADR 0005).
--
-- No backfill/NOT-NULL split needed here, unlike `0013`-`0015`: this table is
-- brand new in M2A and starts empty, so `organization_id NOT NULL` is safe
-- from the first row.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_api_keys (
    key_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    label              TEXT NOT NULL,
    key_prefix         TEXT NOT NULL UNIQUE,
    key_hash           TEXT NOT NULL,
    scopes             TEXT[] NOT NULL DEFAULT '{}',
    created_by_user_id UUID NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at       TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    CONSTRAINT organization_api_keys_scopes_check CHECK (
        scopes <@ ARRAY['leads:write', 'leads:read', 'reviews:read', 'reviews:write']::TEXT[]
    )
);

-- The list-keys-for-an-organization query (GET /api-keys); partial on active
-- keys since that is what every read after key creation actually wants —
-- revoked keys stay in the table for audit but drop out of this index.
CREATE INDEX IF NOT EXISTS idx_organization_api_keys_org
    ON organization_api_keys(organization_id) WHERE revoked_at IS NULL;

-- RLS as defense-in-depth, matching every other tenant-owned table added in
-- Productization M1 (`migrations/0016_row_level_security.sql`) — the API's
-- pooled connection is a service-role/superuser and bypasses this, so the
-- real control is the application-layer organization_id filtering in
-- `arie.apikeys`. Only owner/admin may read or write this table at all: an
-- API key managing other API keys (including revoking the one authenticating
-- the very request) is exactly the escalation surface `arie.api.main`'s
-- `_require_org_admin` also refuses at the application layer, restated here
-- for the same non-bypassing-role scenario `0016` exists for.
ALTER TABLE organization_api_keys ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_api_keys_admin_only ON organization_api_keys;
CREATE POLICY organization_api_keys_admin_only ON organization_api_keys FOR ALL
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));
