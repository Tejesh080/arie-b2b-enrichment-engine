-- =============================================================================
-- 0012_organizations_and_members.sql — Productization M1, secure tenancy
-- foundation: the two root tables everything else scopes against.
--
-- Corrected tenancy boundary (supersedes the shared-evidence-cache design
-- floated during planning): `companies` is the *only* table that stays
-- global. Every other identity/decision/ledger table gets an `organization_id`
-- in follow-up migrations, including `evidence` for company-entity rows — two
-- organizations enriching the same domain each pay for and store their own
-- copy. BYOK/provider licensing, provenance, and cost attribution outweigh the
-- cross-tenant cache saving; a platform-wide shared-evidence cache is a
-- separate, explicit design for if/when platform-wide provider licensing
-- exists.
--
-- `organization_members.user_id` deliberately carries no foreign key to
-- `auth.users`. That table is Supabase-managed and does not exist on a plain
-- local/CI Postgres (`docker-compose.yml`'s `db` service, or the integration
-- suite's `arie_test` database) — migrations must stay re-runnable against
-- both (ADR 0005), so "is this a real user" is enforced at the application
-- layer (`arie.auth`, verifying the Supabase JWT) rather than by a DB
-- constraint only one of the two environments could satisfy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organizations (
    organization_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organization_members (
    organization_id UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    user_id         UUID NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'analyst_reviewer')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);

-- The lookup `arie.auth` runs on every authenticated request: "which orgs is
-- this user an active member of, and at what role" — see arie_current_organization_ids()
-- (0016) for the RLS-side mirror of the same query.
CREATE INDEX IF NOT EXISTS idx_organization_members_user
    ON organization_members(user_id) WHERE status = 'active';
