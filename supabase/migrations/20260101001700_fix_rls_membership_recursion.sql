-- =============================================================================
-- 0018_fix_rls_membership_recursion.sql — stop arie_has_role()/
-- arie_current_organization_ids() from recursing into their own RLS policies.
--
-- ROOT CAUSE (confirmed by reading 0016 directly, and reproduced locally
-- against a real `auth.uid()` + non-bypassing `authenticated` role — see
-- tests/integration/test_rls_membership_recursion.py):
--
-- `organization_members` carries two policies that both apply to a plain
-- SELECT: `org_members_select` (FOR SELECT) and `org_members_write` (FOR ALL,
-- which covers SELECT too). Both functions were `SECURITY INVOKER` (plpgsql's
-- implicit default) plpgsql functions that themselves SELECT from
-- `organization_members`. SECURITY INVOKER means that inner SELECT runs with
-- the *caller's* privileges, so for any role that does not bypass RLS (i.e.
-- any real Supabase `authenticated` client — never the API's own service-role
-- connection, which bypasses RLS entirely per 0016's own docstring) it is
-- itself subject to the same two policies:
--
--   SELECT ... FROM organization_members                  -- any read
--     -> evaluates org_members_select.USING
--          -> calls arie_current_organization_ids()
--               -> SELECT ... FROM organization_members   -- re-enters RLS
--     -> evaluates org_members_write.USING (FOR ALL covers SELECT)
--          -> calls arie_has_role()
--               -> SELECT ... FROM organization_members   -- re-enters RLS
--
-- Each re-entry re-evaluates both policies again, calling the same functions
-- again, with no termination condition — unbounded recursion ending in
-- Postgres's `stack depth limit exceeded`. This is not narrow to one query
-- shape; it fires for *any* SELECT/INSERT/UPDATE/DELETE against
-- `organization_members` issued by a non-bypassing role, which is exactly
-- what a direct-to-Supabase client (PostgREST, or any future one) is.
--
-- FIX: mark both functions `SECURITY DEFINER`, which makes their internal
-- query run as the function's *owner* instead of the calling role.
-- `organization_members` has never had `FORCE ROW LEVEL SECURITY` set (0016
-- never sets it, and this migration doesn't either), so a table owner already
-- bypasses RLS on it unconditionally — and the owner of these
-- `CREATE OR REPLACE FUNCTION` statements is, by construction, whichever role
-- ran this migration, i.e. the same role that ran 0001-0017 and therefore
-- already owns `organization_members` (the role that ran `CREATE TABLE` in
-- 0012). No new role or grant is needed for that part; it falls out of
-- `scripts/migrate.py` always running as one connection for the whole
-- migration history. `CREATE OR REPLACE FUNCTION` does not change an existing
-- function's owner or grants, only its body/attributes, so this is safe to
-- apply on top of whatever owner 0016 already produced.
--
-- SECURITY DEFINER hardening (Postgres's own documented risk for this
-- feature: a mutable `search_path` lets a caller shadow an unqualified
-- reference with an attacker-controlled object of the same name):
--   - `SET search_path = pg_catalog, pg_temp` pins it to exactly the builtin
--     catalog plus the per-session temp schema — no `public`, so a same-named
--     object created in `public` by any role can never shadow anything this
--     function resolves.
--   - Every reference the body makes is additionally schema-qualified
--     (`public.organization_members`, `auth.uid()`) rather than relying on
--     search_path to find it at all — belt-and-suspenders, not either/or.
--   - `EXECUTE` from `PUBLIC` is revoked and re-granted narrowly to
--     `authenticated` and `anon` only: those are the only roles whose queries
--     ever get RLS-evaluated (and therefore the only roles that ever need to
--     invoke these functions at all — nothing in `src/` calls them directly;
--     grep confirms it). Guarded by `pg_roles` existence checks because
--     neither role exists on a plain local/CI Postgres (`docker-compose.yml`'s
--     `db`, or the integration suite's `arie_test`) — only on Supabase, same
--     constraint 0016 already documents for `auth.uid()` itself.
--
-- What this does NOT change, on purpose (preserve existing semantics, no
-- unrelated schema changes):
--   - `auth.uid()` remains the sole identity source in both functions — no
--     caller-supplied user id is ever accepted.
--   - `arie_has_role()`'s `target_organization_id` argument is still only
--     ever supplied by RLS itself as the row's own `organization_id` column
--     value during per-row policy evaluation, never a client-controlled
--     predicate — a client cannot use it to see rows it doesn't already have
--     an active membership for. Unchanged by this migration.
--   - No policy is added, dropped, or restructured. `org_members_write`
--     still applies to SELECT (via FOR ALL) alongside `org_members_select`;
--     that overlap was already harmless before this fix (an owner/admin is
--     always also an active member, so it never grants visibility
--     `org_members_select` wouldn't already) and stays exactly as-is.
-- =============================================================================

CREATE OR REPLACE FUNCTION arie_current_organization_ids() RETURNS SETOF UUID
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RETURN QUERY
    SELECT organization_id FROM public.organization_members
    WHERE user_id = auth.uid() AND status = 'active';
END;
$$;

CREATE OR REPLACE FUNCTION arie_has_role(target_organization_id UUID, allowed_roles TEXT[])
RETURNS BOOLEAN
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM public.organization_members
        WHERE organization_id = target_organization_id
          AND user_id = auth.uid()
          AND status = 'active'
          AND role = ANY(allowed_roles)
    );
END;
$$;

REVOKE ALL ON FUNCTION arie_current_organization_ids() FROM PUBLIC;
REVOKE ALL ON FUNCTION arie_has_role(UUID, TEXT[]) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION arie_current_organization_ids() TO authenticated';
        EXECUTE 'GRANT EXECUTE ON FUNCTION arie_has_role(UUID, TEXT[]) TO authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION arie_current_organization_ids() TO anon';
        EXECUTE 'GRANT EXECUTE ON FUNCTION arie_has_role(UUID, TEXT[]) TO anon';
    END IF;
END;
$$;
