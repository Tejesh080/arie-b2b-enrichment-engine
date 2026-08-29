-- =============================================================================
-- 0016_row_level_security.sql — RLS as defense-in-depth.
--
-- The FastAPI backend connects with a service-role/superuser connection
-- string and therefore bypasses RLS entirely (Postgres exempts superusers and
-- BYPASSRLS roles from row security by definition) — the primary tenant
-- isolation control is the application-layer organization_id filtering added
-- alongside this migration (arie.auth, and every endpoint's ownership check).
-- These policies exist for any access path that is *not* that pooled
-- connection: a future direct-to-Supabase client, the Supabase dashboard, or
-- an application bug that forgets a WHERE clause on a non-bypassing role.
--
-- `arie_current_organization_ids()`/`arie_has_role()` are `LANGUAGE plpgsql`,
-- not `LANGUAGE sql`, deliberately: a `LANGUAGE sql` function's body is parsed
-- and validated against existing catalog objects at CREATE FUNCTION time,
-- which would fail here because `auth.uid()` (Supabase's own function) does
-- not exist on a plain local/CI Postgres — the `db` service in
-- docker-compose.yml and the integration suite's `arie_test` database are
-- both plain Postgres with no `auth` schema at all. plpgsql defers checking
-- the body to first invocation. That invocation only happens for a role that
-- does not bypass RLS; locally `POSTGRES_USER=arie` is the initdb superuser
-- (the official postgres image always makes it one), which bypasses RLS
-- unconditionally, so `auth.uid()` is never actually called there. This
-- migration is therefore safe and re-runnable in both environments without a
-- local shim for Supabase's `auth` schema.
-- =============================================================================

CREATE OR REPLACE FUNCTION arie_current_organization_ids() RETURNS SETOF UUID AS $$
BEGIN
    RETURN QUERY
    SELECT organization_id FROM organization_members
    WHERE user_id = auth.uid() AND status = 'active';
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION arie_has_role(target_organization_id UUID, allowed_roles TEXT[])
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM organization_members
        WHERE organization_id = target_organization_id
          AND user_id = auth.uid()
          AND status = 'active'
          AND role = ANY(allowed_roles)
    );
END;
$$ LANGUAGE plpgsql STABLE;

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE persons ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE provider_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE voi_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE decision_receipts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_select ON organizations;
CREATE POLICY org_select ON organizations FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS org_members_select ON organization_members;
CREATE POLICY org_members_select ON organization_members FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS org_members_write ON organization_members;
CREATE POLICY org_members_write ON organization_members FOR ALL
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

DROP POLICY IF EXISTS persons_tenant_isolation ON persons;
CREATE POLICY persons_tenant_isolation ON persons FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS evidence_tenant_isolation ON evidence;
CREATE POLICY evidence_tenant_isolation ON evidence FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS leads_tenant_isolation ON leads;
CREATE POLICY leads_tenant_isolation ON leads FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS provider_calls_tenant_isolation ON provider_calls;
CREATE POLICY provider_calls_tenant_isolation ON provider_calls FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS model_calls_tenant_isolation ON model_calls;
CREATE POLICY model_calls_tenant_isolation ON model_calls FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS scores_tenant_isolation ON scores;
CREATE POLICY scores_tenant_isolation ON scores FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS voi_decisions_tenant_isolation ON voi_decisions;
CREATE POLICY voi_decisions_tenant_isolation ON voi_decisions FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS human_reviews_tenant_isolation ON human_reviews;
CREATE POLICY human_reviews_tenant_isolation ON human_reviews FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS decision_receipts_tenant_isolation ON decision_receipts;
CREATE POLICY decision_receipts_tenant_isolation ON decision_receipts FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));
