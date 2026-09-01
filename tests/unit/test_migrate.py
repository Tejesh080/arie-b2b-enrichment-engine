"""Migration discovery, checksumming, target resolution, and CLI parsing —
the parts of scripts/migrate.py that don't need a live database.

The target-resolution half of this file is the regression suite for the
incident described in scripts/migrate.py's module docstring: a bare
`python scripts/migrate.py` applied unreleased migrations to production
because the command had no way to say which database was meant. These tests
assert the *inability* to reach production by accident, so they stub
`migrate()` itself and fail loudly if it is ever called on a path that should
have stopped earlier.

Applying migrations (including true-dry-run zero-write behavior) against a
real Postgres is covered by tests/integration/test_migrate_integration.py
instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import scripts.migrate as migrate_module
from scripts.migrate import (
    MigrationsDirectoryError,
    TargetResolutionError,
    checksum_of,
    describe_target,
    migration_files,
    resolve_target,
)

REAL_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_ALL_DB_VARS = (
    "DATABASE_URL",
    "DATABASE_DIRECT_URL",
    "TEST_DATABASE_URL",
    "TEST_DATABASE_DIRECT_URL",
)

PROD_URL = "postgresql://prod_user:prod_secret@db.prod.example:5432/prod_db"
PROD_DIRECT_URL = "postgresql://prod_user:prod_secret@direct.prod.example:5432/prod_db"
TEST_URL = "postgresql://t:t@localhost:5432/arie_test"
TEST_DIRECT_URL = "postgresql://t:t@localhost:5432/arie_test_direct"


@pytest.fixture
def clean_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No database variable set unless a test sets it.

    Load order matters here: `arie.config` calls `load_dotenv()` at import, so
    a developer machine with a populated `.env` already has the production
    variables in `os.environ` by the time any test runs. Without this fixture
    these tests would silently be asserting against that machine's real
    deployment.
    """
    for name in _ALL_DB_VARS:
        monkeypatch.delenv(name, raising=False)


def _no_migrations_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call to `migrate()` a test failure.

    Used by every case whose whole claim is "this invocation performs no
    database work" — a passing assertion on the exit code alone would not
    distinguish "refused" from "ran and happened to return".
    """

    def fail(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError(f"migrate() must not be called (args={args}, kwargs={kwargs})")

    monkeypatch.setattr(migrate_module, "migrate", fail)


def _record_migrate(monkeypatch: pytest.MonkeyPatch, applied: list[str]) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_migrate(conninfo: str, *, dry_run: bool = False) -> list[str]:
        captured["conninfo"] = conninfo
        captured["dry_run"] = dry_run
        return applied

    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)
    return captured


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


# ------------------------------------------------------- target resolution --
#
# Requirements 2, 3, 6 and 7 of the corrective fix: each target resolves from
# its own environment family and *only* its own family. The two cross-family
# cases are the incident itself, in both directions.


def test_test_target_resolves_test_variables(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DATABASE_DIRECT_URL", TEST_DIRECT_URL)
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_URL)

    resolved = resolve_target("test")

    assert resolved.conninfo == TEST_DIRECT_URL
    assert resolved.variable == "TEST_DATABASE_DIRECT_URL"


def test_test_target_falls_back_to_the_pooled_test_url(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For an ordinary Postgres the pooled/direct split is meaningless — the
    two URLs are the same server — so the test target accepts either."""
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_URL)

    resolved = resolve_target("test")

    assert resolved.conninfo == TEST_URL
    assert resolved.variable == "TEST_DATABASE_URL"
    assert resolved.pooled_fallback is True


def test_production_target_resolves_production_variables(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)

    resolved = resolve_target("production")

    assert resolved.conninfo == PROD_DIRECT_URL
    assert resolved.variable == "DATABASE_DIRECT_URL"
    assert resolved.pooled_fallback is False


def test_exported_test_variables_cannot_redirect_the_production_target(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, stated as a test.

    A session had TEST_* exported and believed that aimed the tooling at a
    scratch database. `--target production` must ignore them completely: it
    resolves the production URL, not the test one, and with no production URL
    configured it fails rather than borrowing theirs.
    """
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_URL)
    monkeypatch.setenv("TEST_DATABASE_DIRECT_URL", TEST_DIRECT_URL)

    with pytest.raises(TargetResolutionError, match="DATABASE_DIRECT_URL"):
        resolve_target("production")

    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    assert resolve_target("production").conninfo == PROD_DIRECT_URL


def test_production_variables_cannot_redirect_the_test_target(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse, and the one that actually destroys data: a populated `.env`
    must never let `--target test` resolve to the deployment."""
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)

    with pytest.raises(TargetResolutionError, match="TEST_DATABASE_DIRECT_URL"):
        resolve_target("test")


def test_test_target_refuses_a_url_pointing_at_the_production_database(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separate variables are not enough on their own — someone can paste the
    production URL into TEST_DATABASE_URL. Compare host/port/database and
    refuse, which is the same guard scripts/test_db.py applies."""
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    monkeypatch.setenv("TEST_DATABASE_URL", PROD_URL)

    with pytest.raises(TargetResolutionError, match="same database as DATABASE_URL"):
        resolve_target("test")


def test_unknown_target_is_rejected_by_resolution_too(clean_db_env: None) -> None:
    """argparse `choices` already blocks this from the CLI; the function is
    also importable, so it validates rather than trusting its caller."""
    with pytest.raises(TargetResolutionError, match="unknown target"):
        resolve_target("staging")


# --------------------------------------------------------- identity output --


def test_describe_target_shows_host_and_database_without_the_password(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)

    line = describe_target(resolve_target("production"))

    assert "direct.prod.example:5432/prod_db" in line
    assert "DATABASE_DIRECT_URL" in line
    assert "prod_secret" not in line


def test_describe_target_never_leaks_a_libpq_keyvalue_password(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`urlsplit` on a key/value connection string yields the whole string as
    `path`, so a naive "parse and print" would echo the password verbatim.
    Only host/port/dbname are ever extracted."""
    monkeypatch.setenv(
        "DATABASE_DIRECT_URL",
        "host=direct.prod.example port=5432 dbname=prod_db user=prod password=prod_secret",
    )

    line = describe_target(resolve_target("production"))

    assert "direct.prod.example:5432/prod_db" in line
    assert "prod_secret" not in line


# ------------------------------------------------------------- CLI parsing --
#
# Two bugs are regression-tested here. The older one: `main()` used to never
# call argparse at all, so `--dry-run` was silently accepted and ignored and a
# "dry run" applied migrations for real. The newer one: there was no `--target`
# at all, so a bare invocation wrote to production. Every case stubs `migrate()`
# (no real database involved) and asserts on what `main()` decided to do.


def test_bare_invocation_cannot_touch_production(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Requirement 1, and the whole point of the fix. Production credentials
    are present and valid; the command still must not connect."""
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    _no_migrations_allowed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main([])

    assert exc_info.value.code != 0
    assert "--target" in capsys.readouterr().err


def test_target_without_a_mode_is_an_error(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming the database is necessary but not sufficient: `--apply` is never
    implied, so a half-typed command cannot write either."""
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    _no_migrations_allowed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--target", "production"])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "--dry-run" in err and "--apply" in err


def test_production_apply_requires_explicit_acknowledgement(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    _no_migrations_allowed(monkeypatch)

    exit_code = migrate_module.main(["--target", "production", "--apply"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "--confirm-production-write" in err


def test_production_apply_with_acknowledgement_runs(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    captured = _record_migrate(monkeypatch, ["0034_example.sql"])

    exit_code = migrate_module.main(
        ["--target", "production", "--apply", "--confirm-production-write"]
    )

    assert exit_code == 0
    assert captured["conninfo"] == PROD_DIRECT_URL
    assert captured["dry_run"] is False
    out = capsys.readouterr().out
    assert "direct.prod.example:5432/prod_db" in out
    assert "prod_secret" not in out
    assert "Applied 1 migration(s):" in out


def test_production_dry_run_needs_no_acknowledgement_and_writes_nothing(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Requirement 4 at the CLI layer: a production dry run is allowed without
    ceremony precisely because it is read-only — `migrate()` is called with
    `dry_run=True`, whose zero-write behavior against a real database is
    proved in tests/integration/test_migrate_integration.py."""
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    captured = _record_migrate(monkeypatch, ["0034_example.sql"])

    exit_code = migrate_module.main(["--target", "production", "--dry-run"])

    assert exit_code == 0
    assert captured["dry_run"] is True
    out = capsys.readouterr().out
    assert "Would apply 1 migration(s):" in out
    assert "0034_example.sql" in out
    assert "Applied" not in out


def test_production_apply_over_the_pooled_url_is_refused(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DDL through Supabase's transaction pooler is the hazard the direct URL
    exists to avoid. A dry run over the same connection is fine — it reads."""
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    _no_migrations_allowed(monkeypatch)

    exit_code = migrate_module.main(
        ["--target", "production", "--apply", "--confirm-production-write"]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "DATABASE_DIRECT_URL is not set" in err
    assert "prod_secret" not in err


def test_production_dry_run_over_the_pooled_url_is_allowed(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    captured = _record_migrate(monkeypatch, [])

    assert migrate_module.main(["--target", "production", "--dry-run"]) == 0
    assert captured["conninfo"] == PROD_URL
    assert captured["dry_run"] is True


def test_test_target_apply_uses_the_test_url(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No acknowledgement flag, and no pooled-connection refusal: neither
    applies to a scratch database on an ordinary Postgres."""
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_URL)
    captured = _record_migrate(monkeypatch, [])

    assert migrate_module.main(["--target", "test", "--apply"]) == 0
    assert captured["conninfo"] == TEST_URL
    assert captured["dry_run"] is False


def test_unconfigured_target_is_reported_before_connecting(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_migrations_allowed(monkeypatch)

    exit_code = migrate_module.main(["--target", "production", "--dry-run"])

    assert exit_code == 1
    assert "DATABASE_DIRECT_URL" in capsys.readouterr().err


def test_help_documents_targets_and_modes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "--target" in out
    assert "--dry-run" in out
    assert "--apply" in out
    assert "--confirm-production-write" in out
    assert "TEST_DATABASE_DIRECT_URL" in out
    assert "DATABASE_DIRECT_URL" in out


def test_unknown_option_exits_non_zero_with_a_clear_error(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    _no_migrations_allowed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--target", "production", "--apply", "--unknown-option"])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "unrecognized arguments" in err
    assert "--unknown-option" in err


def test_unknown_target_value_exits_non_zero(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_migrations_allowed(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        migrate_module.main(["--target", "staging", "--dry-run"])

    assert exc_info.value.code != 0
    assert "invalid choice" in capsys.readouterr().err


def test_checksum_mismatch_from_migrate_is_reported_cleanly(
    clean_db_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_migrate(conninfo: str, *, dry_run: bool = False) -> list[str]:
        raise RuntimeError("0001_init.sql was already applied with a different checksum.")

    monkeypatch.setenv("DATABASE_DIRECT_URL", PROD_DIRECT_URL)
    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)

    exit_code = migrate_module.main(["--target", "production", "--dry-run"])

    assert exit_code == 1
    assert "different checksum" in capsys.readouterr().err
