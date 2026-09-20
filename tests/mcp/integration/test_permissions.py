"""Proves the ``arie_mcp_readonly`` role is exactly as narrow as
migrations/0039 and specs/mcp-engineering-interface.md § 7 claim: every
``mcp_diag`` view is readable, every ``public.*`` base table is not, no
write of any kind succeeds anywhere, and a vault-shaped schema is not
reachable either — a database-level property, not something the
application layer merely chooses not to exercise.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

pytestmark = pytest.mark.integration

_MCP_DIAG_VIEWS = (
    "v_schema_migrations",
    "v_queue_depth",
    "v_worker_heartbeats",
    "v_job_detail",
    "v_failed_jobs",
    "v_provider_call_errors",
    "v_provider_quota_signal",
    "v_provider_activity",
    "v_cost_rollup",
    "v_voi_decisions",
    "v_lead_summary",
    "v_organization_summary",
    "v_organization_billing_summary",
)


@pytest.fixture
def readonly_conn(mcp_readonly_database_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(mcp_readonly_database_url, autocommit=True) as conn:
        yield conn


@pytest.mark.parametrize("view_name", _MCP_DIAG_VIEWS)
def test_every_mcp_diag_view_is_readable(readonly_conn: psycopg.Connection, view_name: str) -> None:
    with readonly_conn.cursor() as cur:
        cur.execute(f"SELECT * FROM mcp_diag.{view_name} LIMIT 1")
        cur.fetchall()  # must not raise


@pytest.mark.parametrize(
    "table_name",
    [
        "leads",
        "jobs",
        "persons",
        "companies",
        "provider_calls",
        "voi_decisions",
        "organizations",
        "organization_billing",
        "worker_heartbeats",
        "schema_migrations",
    ],
)
def test_public_base_tables_are_not_directly_selectable(
    readonly_conn: psycopg.Connection, table_name: str
) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege), readonly_conn.cursor() as cur:
        cur.execute(f"SELECT * FROM public.{table_name} LIMIT 1")


def _vault_schema_exists(admin_conn: psycopg.Connection) -> bool:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'vault'")
        return cur.fetchone() is not None


@pytest.mark.parametrize("vault_table", ["secrets", "decrypted_secrets"])
def test_vault_tables_are_not_reachable(
    admin_conn: psycopg.Connection, readonly_conn: psycopg.Connection, vault_table: str
) -> None:
    """This environment already carries a real Supabase Vault installation
    (``vault.secrets``/``vault.decrypted_secrets`` — the same objects
    ``src/arie/vault.py`` talks to), so this exercises migrations/0039's
    guarded ``REVOKE ALL ON SCHEMA vault`` against the genuine article
    rather than a synthetic stand-in. Where a plain local/CI Postgres has no
    Vault extension at all (migrations/0039's own docstring), the guard's
    ``IF EXISTS`` simply never fires — nothing to revoke from a role that
    was never granted anything there in the first place — so this test is
    skipped rather than asserting a schema that structurally can't exist.
    """
    if not _vault_schema_exists(admin_conn):
        pytest.skip("this Postgres has no vault schema installed — nothing to assert")

    expected = (psycopg.errors.InsufficientPrivilege, psycopg.errors.UndefinedTable)
    with pytest.raises(expected), readonly_conn.cursor() as cur:
        cur.execute(f"SELECT * FROM vault.{vault_table} LIMIT 1")


_WRITE_BLOCKED = (
    # Which of these fires depends on *why* Postgres refuses the write —
    # the read-only session setting (default_transaction_read_only), a
    # missing grant, or (for a join view like v_job_detail) the view simply
    # not being structurally updatable. All three are "the write did not
    # happen"; this test asserts that outcome, not the specific mechanism.
    psycopg.errors.ReadOnlySqlTransaction,
    psycopg.errors.InsufficientPrivilege,
    psycopg.errors.ObjectNotInPrerequisiteState,
)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO public.jobs (job_type, status) VALUES ('compute_score', 'pending')",
        "UPDATE public.jobs SET status = 'done'",
        "DELETE FROM public.jobs",
        "INSERT INTO mcp_diag.v_job_detail (job_type) VALUES ('x')",
        "TRUNCATE public.jobs",
    ],
)
def test_no_write_of_any_kind_succeeds(readonly_conn: psycopg.Connection, statement: str) -> None:
    with pytest.raises(_WRITE_BLOCKED), readonly_conn.cursor() as cur:
        cur.execute(statement)


def test_role_session_is_read_only_by_default(readonly_conn: psycopg.Connection) -> None:
    """Belt-and-suspenders check (spec § 7.1): even a table this role *did*
    have a grant on could never be written to, because the session itself
    is read-only — not merely "nothing was granted"."""
    with readonly_conn.cursor() as cur:
        cur.execute("SHOW default_transaction_read_only")
        row = cur.fetchone()
        assert row is not None
    assert row[0] == "on"


def test_statement_timeout_role_default_is_five_seconds(readonly_conn: psycopg.Connection) -> None:
    with readonly_conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        row = cur.fetchone()
        assert row is not None
    assert row[0] == "5s"


def test_organization_billing_summary_view_excludes_stripe_columns(
    readonly_conn: psycopg.Connection,
) -> None:
    """Schema-level check, not a runtime probe: even if a future edit to
    the view's SELECT list were reviewed carelessly, this fails the moment
    a Stripe column becomes selectable — it doesn't depend on a row
    existing to catch it.
    """
    with readonly_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'mcp_diag' AND table_name = 'v_organization_billing_summary'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert columns == {"organization_id", "plan", "status"}
    assert "stripe_customer_id" not in columns
    assert "stripe_subscription_id" not in columns


def test_provider_call_errors_view_excludes_raw_response_ref(
    readonly_conn: psycopg.Connection,
) -> None:
    with readonly_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'mcp_diag' AND table_name = 'v_provider_call_errors'"
        )
        columns = {row[0] for row in cur.fetchall()}
    assert "raw_response_ref" not in columns
