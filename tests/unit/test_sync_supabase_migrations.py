"""scripts/sync_supabase_migrations.py — the migrations/ -> supabase/migrations/ mirror.

No database here: this pins the pure filename/content logic. Applying the
generated mirror to an actual fresh Postgres schema (simulating a real preview
branch) was verified manually against the live database — see
docs/adr/0005-migration-source-of-truth.md — and isn't repeated here since it
would just be re-testing that migrations/ itself is idempotent, which
tests/integration/test_migrate_integration.py already covers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.sync_supabase_migrations import check, planned_mirror, planned_mirrors, sync


def _write(dir_path: Path, name: str, content: str) -> Path:
    path = dir_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_planned_mirror_derives_a_timestamped_name(tmp_path: Path) -> None:
    source = _write(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    name, content = planned_mirror(source)

    assert name.endswith("_init.sql")
    assert len(name) == len("YYYYMMDDHHMMSS_init.sql")
    assert name[:14].isdigit()
    assert content == "CREATE TABLE foo (id INT);"


def test_sequence_order_is_preserved_in_timestamps(tmp_path: Path) -> None:
    a = _write(tmp_path, "0001_first.sql", "-- a")
    b = _write(tmp_path, "0002_second.sql", "-- b")

    name_a, _ = planned_mirror(a)
    name_b, _ = planned_mirror(b)

    assert name_a < name_b


def test_regeneration_is_byte_identical(tmp_path: Path) -> None:
    source = _write(tmp_path, "0001_init.sql", "CREATE TABLE foo (id INT);")
    assert planned_mirror(source) == planned_mirror(source)


def test_rejects_a_filename_outside_the_nnnn_name_convention(tmp_path: Path) -> None:
    bad = _write(tmp_path, "init.sql", "CREATE TABLE foo (id INT);")
    with pytest.raises(ValueError):
        planned_mirror(bad)


def test_sync_writes_exactly_the_planned_mirror(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")
    _write(source_dir, "0002_views.sql", "CREATE VIEW bar AS SELECT 1;")

    written = sync(target_dir, source_dir)

    assert len(written) == 2
    existing = {p.name: p.read_text(encoding="utf-8") for p in target_dir.glob("*.sql")}
    assert existing == planned_mirrors(source_dir)


def test_sync_removes_a_mirror_no_longer_produced_by_a_source_file(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")
    sync(target_dir, source_dir)
    assert len(list(target_dir.glob("*.sql"))) == 1

    (source_dir / "0001_init.sql").unlink()
    sync(target_dir, source_dir)

    assert list(target_dir.glob("*.sql")) == []


def test_check_passes_immediately_after_sync(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")

    sync(target_dir, source_dir)

    assert check(target_dir, source_dir) == []


def test_check_reports_a_missing_mirror(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")

    problems = check(target_dir, source_dir)

    assert len(problems) == 1
    assert "missing" in problems[0]


def test_check_reports_stale_content(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")
    sync(target_dir, source_dir)

    name, _ = planned_mirror(next(source_dir.glob("*.sql")))
    _write(target_dir, name, "-- hand-edited, now stale")

    problems = check(target_dir, source_dir)

    assert len(problems) == 1
    assert "stale" in problems[0]


def test_check_reports_an_orphaned_mirror(tmp_path: Path) -> None:
    source_dir = tmp_path / "migrations"
    target_dir = tmp_path / "supabase" / "migrations"
    source_dir.mkdir()
    _write(source_dir, "0001_init.sql", "CREATE TABLE foo (id INT);")
    sync(target_dir, source_dir)

    (source_dir / "0001_init.sql").unlink()

    problems = check(target_dir, source_dir)

    assert len(problems) == 1
    assert "stale" in problems[0]


def test_check_passes_against_the_real_repo_mirror() -> None:
    """The actual migrations/ and supabase/migrations/ committed in this repo must agree."""
    assert check() == []
