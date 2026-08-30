-- =============================================================================
-- 0019_organization_icp_profiles.sql — Productization M3: organization-owned,
-- immutably versioned ICP/scoring configuration.
--
-- Rows are never mutated after creation except the one-time `status`/
-- `retired_at` transition an activation performs on the *previous* active
-- row (see `arie.icp_profiles.activate_profile`) — the `config`/`weights`/
-- `scorer_version` a Decision Receipt records against a lead can never
-- change out from under it, which is what keeps an old receipt reproducible.
--
-- `config` is a validated JSON document (`arie.icp_profiles.ICPProfileConfig`),
-- never arbitrary/executable — see that module for the schema and validation
-- (weights, thresholds, per-category point maps, employee-count bands, the
-- disqualifier on/off toggle). `scorer_version` freezes which *algorithm*
-- shape (`arie.scoring.rules.RULES_VERSION`) the config was written for,
-- independent of the weights/thresholds themselves — mirrors
-- `decision_receipts.scorer_version`'s existing meaning.
--
-- One active profile per organization, enforced at the database level by the
-- partial unique index below (not just application logic) — an activation
-- race would otherwise be a lost-update bug, not merely an inconsistency.
--
-- `created_by_user_id` carries no foreign key to `auth.users`, matching
-- `organization_api_keys.created_by_user_id` and `organization_members.
-- user_id` (see `migrations/0012_organizations_and_members.sql`'s note) — and
-- is nullable here specifically because this migration's own bootstrap rows
-- below are system-created, with no real user behind them. Every profile
-- created through the API always populates it (`arie.icp_profiles.
-- create_profile` requires an owner/admin JWT session).
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_icp_profiles (
    profile_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    version            INT NOT NULL CHECK (version >= 1),
    name               TEXT NOT NULL,
    config             JSONB NOT NULL,
    scorer_version     TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    created_by_user_id UUID,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at         TIMESTAMPTZ,
    CONSTRAINT organization_icp_profiles_org_version_unique UNIQUE (organization_id, version)
);

-- The database-level single-active-profile invariant. A partial unique index
-- on a constant expression scoped to `status = 'active'` rows only — a
-- second concurrent activation attempt for the same organization fails this
-- constraint rather than silently leaving two active rows.
CREATE UNIQUE INDEX IF NOT EXISTS idx_organization_icp_profiles_one_active
    ON organization_icp_profiles(organization_id) WHERE status = 'active';

-- Version-history listing (GET /organization/icp/versions), newest first.
CREATE INDEX IF NOT EXISTS idx_organization_icp_profiles_org
    ON organization_icp_profiles(organization_id, version DESC);

-- Bootstrap a version-1 "Reference ICP" profile for every organization that
-- exists as of this migration, transcribing `arie.scoring.rules`'s hardcoded
-- reference constants verbatim into `config` — so the JSONB representation
-- and the module's own defaults can never silently diverge (a unit test,
-- `tests/unit/test_icp_profiles.py`, pins the two together). This closes the
-- brief's own requirement: no existing lead's future reprocessing, and no
-- newly ingested lead for an already-existing organization, should observe
-- any change from this migration alone. `WHERE NOT EXISTS` makes this
-- idempotent and re-runnable (ADR 0005), and also means a fresh organization
-- created *before* this migration but backfilled by a later run still gets
-- exactly one bootstrap profile, never two.
INSERT INTO organization_icp_profiles (
    organization_id, version, name, config, scorer_version, status, created_by_user_id, activated_at
)
SELECT
    o.organization_id,
    1,
    'Reference ICP',
    '{
        "qualify_threshold": 65.0,
        "reject_threshold": 55.0,
        "employee_count_bands": [
            {"min_employees": 1, "max_employees": 10, "points": 2.0},
            {"min_employees": 11, "max_employees": 50, "points": 10.0},
            {"min_employees": 51, "max_employees": 200, "points": 20.0},
            {"min_employees": 201, "max_employees": 1000, "points": 18.0},
            {"min_employees": 1001, "max_employees": 1000000000, "points": 8.0}
        ],
        "industry_points": {
            "software": 15.0, "fintech": 15.0, "healthtech": 13.0, "ecommerce": 12.0,
            "logistics": 8.0, "manufacturing": 7.0, "education": 5.0, "nonprofit": 2.0
        },
        "seniority_points": {
            "c_level": 20.0, "vp": 18.0, "director": 14.0, "manager": 8.0, "ic": 2.0
        },
        "function_points": {
            "data": 15.0, "engineering": 14.0, "operations": 9.0, "marketing": 5.0,
            "sales": 5.0, "finance": 4.0, "other": 2.0
        },
        "buying_intent_weight": 20.0,
        "trigger_event_weight": 10.0,
        "target_geographies": [],
        "disqualifier_enabled": true
    }'::jsonb,
    'icp-1.0.0',
    'active',
    NULL,
    now()
FROM organizations o
WHERE NOT EXISTS (
    SELECT 1 FROM organization_icp_profiles p WHERE p.organization_id = o.organization_id
);

-- RLS as defense-in-depth, matching every other tenant-owned table
-- (`migrations/0016_row_level_security.sql`). Read is open to any active
-- member (any role) — configuration visibility is not an admin-only concern,
-- only *changing* it is. Writes (creating a new version) require owner/admin,
-- mirroring `organization_api_keys`'s admin-only policy. No DELETE policy is
-- defined at all: these rows are permanent audit history, and RLS's default
-- deny-if-no-policy-matches means even a non-bypassing role can never delete
-- one, on top of the application layer never attempting to.
ALTER TABLE organization_icp_profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_icp_profiles_select ON organization_icp_profiles;
CREATE POLICY organization_icp_profiles_select ON organization_icp_profiles FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS organization_icp_profiles_admin_insert ON organization_icp_profiles;
CREATE POLICY organization_icp_profiles_admin_insert ON organization_icp_profiles FOR INSERT
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

DROP POLICY IF EXISTS organization_icp_profiles_admin_update ON organization_icp_profiles;
CREATE POLICY organization_icp_profiles_admin_update ON organization_icp_profiles FOR UPDATE
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));
