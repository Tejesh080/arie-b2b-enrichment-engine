"""Migration discovery and checksumming — the parts of scripts/migrate.py that
don't need a live database.

Applying migrations against a real Postgres is covered by
tests/integration/test_migrate_integration.py instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.migrate import MigrationsDirectoryError, checksum_of, migration_files

REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def test_migration_files_are_found_in_the_repo() -> None:
    files = migration_files(REAL_MIGRATIONS_DIR)
    names = [f.name for f in files]
    assert "0001_init.sql" in names
    assert "0002_metrics_views.sql" in names


def test_migration_files_are_sorted_by_filename() -> None:
    files = migration_files(REAL_MIGRATIONS_DIR)
    names = [f.name for f in files]
    assert names == sorted(names)


def test_checksum_is_deterministic() -> None:
    sql = "CREATE TABLE IF NOT EXISTS foo (id INT);"
    assert checksum_of(sql) == checksum_of(sql)


def test_checksum_changes_with_content() -> None:
    a = checksum_of("CREATE TABLE a (id INT);")
    b = checksum_of("CREATE TABLE b (id INT);")
    assert a != b


def test_migration_files_ignores_non_sql(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0002_next.sql").write_text("SELECT 2;", encoding="utf-8")
    (tmp_path / "README.md").write_text("not a migration", encoding="utf-8")

    files = migration_files(tmp_path)

    assert [f.name for f in files] == ["0001_init.sql", "0002_next.sql"]


def test_migration_files_fails_closed_on_a_missing_directory(tmp_path: Path) -> None:
    """The audit-fixed bug: `Path.glob()` on a nonexistent directory returns
    `[]`, not an error — which used to make a wrong `MIGRATIONS_DIR` read as
    "nothing pending" instead of "I can't tell." Must raise, not return []."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(MigrationsDirectoryError, match="not found"):
        migration_files(missing)


def test_migration_files_fails_closed_on_an_empty_directory(tmp_path: Path) -> None:
    """A directory that exists but has zero .sql files is just as suspect as
    one that doesn't exist — this repo always ships at least 0001_init.sql."""
    with pytest.raises(MigrationsDirectoryError, match="no \\*\\.sql"):
        migration_files(tmp_path)
