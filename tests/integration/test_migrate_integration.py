"""Migrations applied against a real Postgres database.

The schema has "never been run against a live database" per
docs/architecture.md — this is what closes that gap. Requires
DATABASE_URL / DATABASE_DIRECT_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from scripts.migrate import checksum_of, migrate, migration_files

from arie.config import DatabaseConfig
from arie.migrations import MigrationsDirectoryError, pending_migrations

pytestmark = pytest.mark.integration


def test_migrating_twice_is_idempotent(migrated_database: str) -> None:
    # `migrated_database` (session-scoped) already applied everything once.
    db = DatabaseConfig()
    applied_again = migrate(db.direct_url)
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
                entity_type, entity_id, field_name, value, source, confidence, ttl_seconds
            ) VALUES ('company', %s, 'industry', '"fintech"', 'test', 0.9, 3600)
            RETURNING fetched_at, expires_at
            """,
            (entity_id,),
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


def test_reapplying_a_changed_migration_raises(
    migrated_database: str, db_conn: psycopg.Connection
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE schema_migrations SET checksum = 'deliberately-wrong' WHERE filename = %s",
            ("0001_init.sql",),
        )
    db_conn.commit()

    db = DatabaseConfig()
    try:
        with pytest.raises(RuntimeError, match="checksum"):
            migrate(db.direct_url)
    finally:
        # Restore the real checksum so later tests / re-runs aren't left broken.
        real = next(f for f in migration_files() if f.name == "0001_init.sql")
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE filename = %s",
                (checksum_of(real.read_text(encoding="utf-8")), "0001_init.sql"),
            )
        db_conn.commit()
