"""Migration discovery, checksumming, and CLI parsing — the parts of
scripts/migrate.py that don't need a live database.

Applying migrations (including true-dry-run zero-write behavior) against a
real Postgres is covered by tests/integration/test_migrate_integration.py
instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.migrate as migrate_module
from scripts.migrate import MigrationsDirectoryError, checksum_of, migration_files

from arie.config import DatabaseConfig

REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def _with_direct_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Swap in a fresh `DatabaseConfig` — `DATABASE` is a frozen dataclass
    singleton, so its own `direct_url` field can't be monkeypatched in place."""
    monkeypatch.setattr(migrate_module, "DATABASE", DatabaseConfig(direct_url=url))


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


# ------------------------------------------------------------- CLI parsing --
#
# The bug this section regression-tests: `main()` used to never call
# `sys.argv`/`argparse` at all, so `--dry-run` was silently accepted and
# ignored — a "dry run" applied migrations for real. Every case here stubs
# out `migrate()` itself (no real database involved) and asserts only on
# what `main()` decided to pass it and print, which is exactly the part that
# was broken.


def test_dry_run_flag_is_parsed_and_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_migrate(conninfo: str, *, dry_run: bool = False) -> list[str]:
        captured["dry_run"] = dry_run
        return ["0023_example.sql"]

    _with_direct_url(monkeypatch, "postgresql://example/db")
    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)

    exit_code = migrate_module.main(["--dry-run"])

    assert exit_code == 0
    assert captured["dry_run"] is True


def test_no_args_applies_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_migrate(conninfo: str, *, dry_run: bool = False) -> list[str]:
        captured["dry_run"] = dry_run
        return []

    _with_direct_url(monkeypatch, "postgresql://example/db")
    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)

    exit_code = migrate_module.main([])

    assert exit_code == 0
    assert captured["dry_run"] is False


def test_dry_run_output_says_would_apply_not_applied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_direct_url(monkeypatch, "postgresql://example/db")
    monkeypatch.setattr(
        migrate_module, "migrate", lambda conninfo, *, dry_run=False: ["0023_example.sql"]
    )

    migrate_module.main(["--dry-run"])

    out = capsys.readouterr().out
    assert "Would apply 1 migration(s):" in out
    assert "0023_example.sql" in out
    assert "Applied" not in out


def test_help_documents_usage_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out


def test_unknown_option_exits_non_zero_with_a_clear_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--unknown-option"])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--unknown-option" in err


def test_missing_database_direct_url_is_reported_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _with_direct_url(monkeypatch, "")

    def fail_if_called(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("migrate() must not be called with no DATABASE_DIRECT_URL")

    monkeypatch.setattr(migrate_module, "migrate", fail_if_called)

    exit_code = migrate_module.main(["--dry-run"])

    assert exit_code == 1
    assert "DATABASE_DIRECT_URL is not set" in capsys.readouterr().err


def test_checksum_mismatch_from_migrate_is_reported_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_migrate(conninfo: str, *, dry_run: bool = False) -> list[str]:
        raise RuntimeError("0001_init.sql was already applied with a different checksum.")

    _with_direct_url(monkeypatch, "postgresql://example/db")
    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)

    exit_code = migrate_module.main(["--dry-run"])

    assert exit_code == 1
    assert "different checksum" in capsys.readouterr().err
