"""0018_fix_rls_membership_recursion.sql — proof the recursion is gone.

`migrations/0016_row_level_security.sql` made `arie_current_organization_ids()`
and `arie_has_role()` `SECURITY INVOKER` plpgsql functions that each SELECT
from `organization_members` — a table whose own RLS policies call those same
functions. For any role that does not bypass RLS, that inner SELECT re-enters
the same policies, which call the same functions again, without end
(`stack depth limit exceeded`). The API's own connection never hits this: it
is a service-role/superuser connection that bypasses RLS entirely (see 0016's
docstring) and the backend never calls these functions directly (nothing under
`src/` references them). Only a direct-to-Supabase client — a real
`authenticated` PostgREST request, which is exactly what `arie-web`'s frontend
started doing in the Supabase auth work this migration follows — ever took
this path, which is how it went unnoticed until then.

None of this is reachable through `migrated_database`'s connection, which is
always the local Postgres superuser and therefore always bypasses RLS
regardless of what these functions do (0016's own docstring). Exercising the
actual bug needs the two things a plain local/CI Postgres does not otherwise
have: a real `auth.uid()` and a role that does not bypass row security. Both
are built manually here rather than assumed present, deliberately mirroring
Supabase's real shape rather than the API's own bypass-everything path:

* `auth.uid()` — Supabase implements it as reading the `sub` claim out of a
  `request.jwt.claims` GUC that PostgREST sets per request. The stub function
  below is exactly that implementation, not an approximation of it.
* `authenticated` / `anon` — created as plain `NOBYPASSRLS` roles, matching
  Supabase's own roles in every property RLS cares about.
* The per-test transaction setup (`set_config('request.jwt.claims', ..., true)`
  then `SET LOCAL ROLE`) is the same sequence PostgREST performs per request,
  not a simplification of it.

`rls_test_roles` (via `_CREATE_AUTH_STUB`) does not depend on
`migrations/0018_fix_rls_membership_recursion.sql` having already granted
`EXECUTE` to `authenticated`/`anon` — that migration's own grant only fires if
those roles already exist *when the migration runs* (true on real Supabase,
where they predate every migration; not necessarily true here, where this
file is what first creates them against a given local test database). The
grant is therefore repeated directly in this file's own setup, independent of
migration timing, so this test exercises the shipped function bodies
(`SECURITY DEFINER`, pinned `search_path`) regardless of which ran first.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration

_CREATE_AUTH_STUB = """
CREATE SCHEMA IF NOT EXISTS auth;

CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID
LANGUAGE sql STABLE
AS $$
  SELECT nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOSUPERUSER NOCREATEDB NOCREATEROLE NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOSUPERUSER NOCREATEDB NOCREATEROLE NOLOGIN NOBYPASSRLS;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO authenticated, anon;
GRANT USAGE ON SCHEMA auth TO authenticated, anon;
GRANT ALL ON ALL TABLES IN SCHEMA public TO authenticated, anon;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO authenticated, anon;

-- Independent of whether 0018 itself already did this — see module docstring.
GRANT EXECUTE ON FUNCTION arie_current_organization_ids() TO authenticated, anon;
GRANT EXECUTE ON FUNCTION arie_has_role(UUID, TEXT[]) TO authenticated, anon;
"""


@pytest.fixture(scope="session")
def rls_test_roles(migrated_database_direct: str) -> None:
    """Create the Supabase-shaped `auth.uid()`/`authenticated`/`anon` stub, once."""
    with psycopg.connect(migrated_database_direct, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_CREATE_AUTH_STUB)


@dataclass
class TwoOrgFixture:
    org_a: UUID
    org_b: UUID
    user_a: UUID  # active owner of org_a only
    user_b: UUID  # active owner of org_b only


@pytest.fixture
def two_orgs(db_conn: psycopg.Connection, rls_test_roles: None) -> Iterator[TwoOrgFixture]:
    org_a, org_b, user_a, user_b = uuid4(), uuid4(), uuid4(), uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, 'RLS Test Org A', %s, 'active'), (%s, 'RLS Test Org B', %s, 'active')",
            (org_a, f"rls-test-a-{org_a.hex[:10]}", org_b, f"rls-test-b-{org_b.hex[:10]}"),
        )
        cur.execute(
            "INSERT INTO organization_members (organization_id, user_id, role, status) "
            "VALUES (%s, %s, 'owner', 'active'), (%s, %s, 'owner', 'active')",
            (org_a, user_a, org_b, user_b),
        )
    db_conn.commit()
    try:
        yield TwoOrgFixture(org_a=org_a, org_b=org_b, user_a=user_a, user_b=user_b)
    finally:
        with db_conn.cursor() as cur:
            # Cascades to organization_members (FK ON DELETE CASCADE, 0012).
            cur.execute(
                "DELETE FROM organizations WHERE organization_id = ANY(%s)", ([org_a, org_b],)
            )
        db_conn.commit()


def _as(cur: psycopg.Cursor, user_id: UUID | None) -> None:
    """Reproduce PostgREST's own per-request transaction setup: the JWT claims
    GUC and the role switch it performs before running the caller's query —
    not an approximation of it, the same two statements in the same order."""
    claims = "{}" if user_id is None else f'{{"sub": "{user_id}"}}'
    cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
    cur.execute("SET LOCAL ROLE authenticated" if user_id else "SET LOCAL ROLE anon")


def test_authenticated_user_can_select_own_membership(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT organization_id FROM organization_members "
            "WHERE user_id = auth.uid() AND status = 'active'"
        )
        rows = cur.fetchall()
    assert rows == [(two_orgs.org_a,)]


def test_authenticated_user_cannot_select_another_users_membership(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT organization_id FROM organization_members WHERE user_id = %s",
            (two_orgs.user_b,),
        )
        rows = cur.fetchall()
    assert rows == []


def test_arie_has_role_true_for_own_active_membership(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    """Also the direct regression check for the reported `stack depth limit
    exceeded` in `arie_has_role` — a matching row is exactly the case that
    previously recursed (see the migration's own root-cause comment)."""
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute("SELECT arie_has_role(%s, ARRAY['owner', 'admin'])", (two_orgs.org_a,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True


def test_arie_has_role_false_for_foreign_organization(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    """A caller-supplied organization id can never expand access: this checks
    org_b while authenticated as a user who only belongs to org_a."""
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute("SELECT arie_has_role(%s, ARRAY['owner', 'admin'])", (two_orgs.org_b,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False


def test_anon_sees_no_membership_rows(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute("SELECT organization_id FROM organization_members")
        rows = cur.fetchall()
    assert rows == []


def test_tenant_scoped_table_isolation_survives_the_fix(
    migrated_database_direct: str, db_conn: psycopg.Connection, two_orgs: TwoOrgFixture
) -> None:
    """`leads`' own RLS policy calls `arie_current_organization_ids()` too
    (0016) — proving a downstream tenant table still isolates correctly is
    proof this fix didn't just move the recursion, it removed it."""
    lead_id = uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (lead_id, source, organization_id) VALUES (%s, 'rls-test', %s)",
            (lead_id, two_orgs.org_b),
        )
    db_conn.commit()
    try:
        with (
            psycopg.connect(migrated_database_direct) as conn,
            conn.transaction(),
            conn.cursor() as cur,
        ):
            _as(cur, two_orgs.user_a)
            cur.execute("SELECT lead_id FROM leads WHERE organization_id = %s", (two_orgs.org_b,))
            rows = cur.fetchall()
        assert rows == []
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE lead_id = %s", (lead_id,))
        db_conn.commit()
