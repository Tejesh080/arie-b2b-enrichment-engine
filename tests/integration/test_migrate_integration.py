"""Migrations applied against a real Postgres database.

The schema has "never been run against a live database" per
docs/architecture.md — this is what closes that gap. Requires
DATABASE_URL / DATABASE_DIRECT_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
import pytest
import scripts.migrate as migrate_module
from scripts.migrate import checksum_of, migrate, migration_files

from arie.migrations import MigrationsDirectoryError, pending_migrations
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration


def test_migrating_twice_is_idempotent(migrated_database_direct: str) -> None:
    # `migrated_database` (session-scoped) already applied everything once.
    applied_again = migrate(migrated_database_direct)
    assert applied_again == []


def test_core_tables_exist(db_conn: psycopg.Connection) -> None:
    expected = {
        "companies",
        "persons",
        "leads",
        "lead_events",
        "evidence",
        "provider_calls",
        "model_calls",
        "voi_decisions",
        "scores",
        "human_reviews",
        "jobs",
        "eval_leads",
        "eval_runs",
        "eval_results",
        "schema_migrations",
    }
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        actual = {row[0] for row in cur.fetchall()}
    missing = expected - actual
    assert not missing, f"tables missing after migration: {missing}"


def test_metrics_views_are_queryable(db_conn: psycopg.Connection) -> None:
    views = (
        "v_lead_cost",
        "v_pipeline_metrics",
        "v_escalation_rate",
        "v_model_escalation",
        "v_provider_health",
    )
    with db_conn.cursor() as cur:
        for view in views:
            cur.execute(f"SELECT * FROM {view} LIMIT 0")


def test_evidence_expires_at_is_generated_from_fetched_at_and_ttl(
    db_conn: psycopg.Connection, cleanup_evidence: list[UUID]
) -> None:
    entity_id = uuid4()
    cleanup_evidence.append(entity_id)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence (
                organization_id, entity_type, entity_id, field_name, value, source, confidence,
                ttl_seconds
            ) VALUES (%s, 'company', %s, 'industry', '"fintech"', 'test', 0.9, 3600)
            RETURNING fetched_at, expires_at
            """,
            (str(LEGACY_ORGANIZATION_ID), entity_id),
        )
        row = cur.fetchone()
        assert row is not None
        fetched_at, expires_at = row
    db_conn.commit()

    assert (expires_at - fetched_at).total_seconds() == pytest.approx(3600, abs=1)


def test_pending_migrations_is_empty_once_fully_migrated(
    migrated_database: str, db_conn: psycopg.Connection
) -> None:
    assert pending_migrations(db_conn) == []


def test_pending_migrations_reports_an_unrecorded_migration(
    migrated_database: str, db_conn: psycopg.Connection
) -> None:
    # Same mutate-then-restore shape as test_reapplying_a_changed_migration_raises
    # below: this is a live, shared database, so the change this test makes has
    # to be invisible to every other test once it finishes.
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM schema_migrations WHERE filename = %s", ("0002_metrics_views.sql",)
        )
    db_conn.commit()

    try:
        assert pending_migrations(db_conn) == ["0002_metrics_views.sql"]
    finally:
        real = next(f for f in migration_files() if f.name == "0002_metrics_views.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                ("0002_metrics_views.sql", checksum_of(real.read_text(encoding="utf-8"))),
            )
        db_conn.commit()


def test_pending_migrations_fails_closed_on_a_bad_migrations_dir(
    migrated_database: str, db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    """The audit-fixed bug, against a real connection: a wrong MIGRATIONS_DIR
    must never read as "fully migrated" — even though the database really is
    fully migrated here, `pending_migrations` cannot know that if it can't
    see the directory describing what "fully migrated" means."""
    with pytest.raises(MigrationsDirectoryError):
        pending_migrations(db_conn, migrations_dir=tmp_path / "does-not-exist")


def _schema_migrations_snapshot(conn: psycopg.Connection) -> set[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename, checksum FROM schema_migrations")
        return {(row[0], row[1]) for row in cur.fetchall()}


def test_dry_run_lists_pending_without_writing(
    migrated_database_direct: str, db_conn: psycopg.Connection
) -> None:
    """The regression test for the M3-rollout bug: a dry run must report what
    is pending, exactly like a real run would, but leave `schema_migrations`
    byte-for-byte unchanged — same mutate-then-restore shape as
    `test_pending_migrations_reports_an_unrecorded_migration`."""
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM schema_migrations WHERE filename = %s", ("0002_metrics_views.sql",)
        )
    db_conn.commit()

    before = _schema_migrations_snapshot(db_conn)
    try:
        applied = migrate(migrated_database_direct, dry_run=True)
        assert applied == ["0002_metrics_views.sql"]

        after = _schema_migrations_snapshot(db_conn)
        assert after == before, "dry run must not write to schema_migrations"
    finally:
        real = next(f for f in migration_files() if f.name == "0002_metrics_views.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                ("0002_metrics_views.sql", checksum_of(real.read_text(encoding="utf-8"))),
            )
        db_conn.commit()


def test_dry_run_matches_pending_migrations(
    migrated_database: str, migrated_database_direct: str, db_conn: psycopg.Connection
) -> None:
    """A dry run and `arie.migrations.pending_migrations` (the function
    `/healthz` itself calls) must never disagree — both describe the same
    "what hasn't run yet" fact from the same on-disk migration list."""
    assert migrate(migrated_database_direct, dry_run=True) == pending_migrations(db_conn)


def test_dry_run_raises_on_checksum_mismatch_without_writing(
    migrated_database_direct: str, db_conn: psycopg.Connection
) -> None:
    """A dry run must catch a corrupted/edited migration exactly like a real
    run does — checksum verification is read-only, so there's no reason a
    dry run should be blind to it. Same mutate-then-restore shape as
    `test_reapplying_a_changed_migration_raises`."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = 'deliberately-wrong' WHERE filename = %s",
            ("0001_init.sql",),
        )
    db_conn.commit()

    before = _schema_migrations_snapshot(db_conn)
    try:
        with pytest.raises(RuntimeError, match="checksum"):
            migrate(migrated_database_direct, dry_run=True)

        after = _schema_migrations_snapshot(db_conn)
        assert after == before, "a raising dry run must still write nothing"
    finally:
        real = next(f for f in migration_files() if f.name == "0001_init.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                (checksum_of(real.read_text(encoding="utf-8")), "0001_init.sql"),
            )
        db_conn.commit()


def test_dry_run_against_a_schema_with_no_schema_migrations_table_creates_nothing(
    migrated_database_direct: str, db_conn: psycopg.Connection, integration_run_id: str
) -> None:
    """The strongest form of the "zero writes" contract: run a dry run
    against a Postgres search_path that has never seen a migration at all,
    and confirm it neither creates the bootstrap `schema_migrations` table
    nor anything else — only ever a `SELECT` that 42P01s and rolls back.

    A fresh *schema* on the same already-migrated test database, rather than
    a second real database, keeps this cheap and self-contained while still
    exercising a real "table doesn't exist yet" `UndefinedTable` path against
    a real server (not a mock).
    """
    schema = f"it_freshschema_{integration_run_id.replace('-', '')}"
    with db_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    db_conn.commit()

    try:
        conninfo = f"{migrated_database_direct}?options={quote(f'-c search_path={schema}')}"

        applied = migrate(conninfo, dry_run=True)

        assert applied == [f.name for f in migration_files()]
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = 'schema_migrations'",
                (schema,),
            )
            assert cur.fetchone() is None, "dry run must not create the bootstrap table"
    finally:
        with db_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        db_conn.commit()


def test_reapplying_a_changed_migration_raises(
    migrated_database_direct: str, db_conn: psycopg.Connection
) -> None:
    """Corrupts a checksum row, then asserts the runner refuses to proceed.

    The corruption and the ``migrate()`` call have to target the *same*
    database, which is why this takes a fixture rather than reading config: it
    previously corrupted the test database and then ran the migration runner
    against production, where the checksum was of course still valid — so the
    test asserted nothing and touched a deployment to do it.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = 'deliberately-wrong' WHERE filename = %s",
            ("0001_init.sql",),
        )
    db_conn.commit()

    try:
        with pytest.raises(RuntimeError, match="checksum"):
            migrate(migrated_database_direct)
    finally:
        # Restore the real checksum so later tests / re-runs aren't left broken.
        real = next(f for f in migration_files() if f.name == "0001_init.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                (checksum_of(real.read_text(encoding="utf-8")), "0001_init.sql"),
            )
        db_conn.commit()


def test_cli_production_dry_run_writes_nothing_against_a_real_database(
    migrated_database_direct: str,
    db_conn: psycopg.Connection,
    integration_run_id: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Requirement 4 of the corrective fix, proved at the layer an operator
    actually types rather than one function below it.

    `test_dry_run_against_a_schema_with_no_schema_migrations_table_creates_
    nothing` proves `migrate(dry_run=True)` writes nothing; this proves
    `main(["--target", "production", "--dry-run"])` reaches that behavior —
    i.e. the CLI resolves the production variable family, needs no
    acknowledgement flag to *look*, and still creates not even the bootstrap
    table. A fresh schema on the test server stands in for production, which
    is the only honest way to assert "zero writes to production" in a test.
    """
    schema = f"it_cli_dryrun_{integration_run_id.replace('-', '')}"
    with db_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    db_conn.commit()

    try:
        conninfo = f"{migrated_database_direct}?options={quote(f'-c search_path={schema}')}"
        monkeypatch.setenv("DATABASE_DIRECT_URL", conninfo)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        exit_code = migrate_module.main(["--target", "production", "--dry-run"])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Mode:   dry run (no writes)" in out
        assert "Would apply" in out

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            assert cur.fetchall() == [], "a production dry run must create no tables at all"
    finally:
        with db_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        db_conn.commit()


def test_cli_production_apply_without_acknowledgement_never_connects(
    migrated_database_direct: str,
    db_conn: psycopg.Connection,
    integration_run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acknowledgement gate is checked before anything opens a connection,
    so an unacknowledged `--apply` against a genuinely migratable database
    still leaves it untouched."""
    schema = f"it_cli_noack_{integration_run_id.replace('-', '')}"
    with db_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {schema}")
    db_conn.commit()

    try:
        conninfo = f"{migrated_database_direct}?options={quote(f'-c search_path={schema}')}"
        monkeypatch.setenv("DATABASE_DIRECT_URL", conninfo)
        monkeypatch.delenv("DATABASE_URL", raising=False)

        assert migrate_module.main(["--target", "production", "--apply"]) == 1

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            assert cur.fetchall() == []
    finally:
        with db_conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema} CASCADE")
        db_conn.commit()
