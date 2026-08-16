"""Generate supabase/migrations/ from migrations/ — the canonical source.

Supabase's GitHub integration (Branching / PR preview databases) watches
``supabase/migrations/<timestamp>_<name>.sql``, the Supabase CLI's own
convention — a different directory, and a different tracking table
(``supabase_migrations.schema_migrations``), than this project's own
``migrations/`` + ``scripts/migrate.py`` + ``public.schema_migrations``, which
predates Supabase Branching being enabled on this repo and remains the only
path that touches the production database. See
``docs/adr/0005-migration-source-of-truth.md`` for why both exist.

This script makes the same SQL available under both conventions without
forking it: ``migrations/`` stays the single source of truth, and
``supabase/migrations/`` is a generated, byte-identical mirror. Never
hand-edit a file under ``supabase/migrations/`` — edit ``migrations/`` and
regenerate with ``make sync-supabase-migrations``. ``make
check-supabase-migrations`` (wired into CI) fails the build if the two ever
drift apart.

Timestamps are synthetic, derived purely from each file's own numeric
sequence prefix (``0001``, ``0002``, ...) rather than from git history or wall
-clock time, so regenerating is deterministic — the same ``migrations/``
directory always produces byte-identical output — and possible even for a
migration that hasn't been committed yet.
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "migrations"
TARGET_DIR = REPO_ROOT / "supabase" / "migrations"

_FILENAME_RE = re.compile(r"^(\d{4})_(.+)\.sql$")

# Arbitrary fixed epoch — only the *ordering* (ascending, one per sequence
# number) matters to Supabase's tooling, not the value itself.
_SYNTHETIC_BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _timestamp_for(sequence: int) -> str:
    stamp = _SYNTHETIC_BASE + timedelta(minutes=sequence - 1)
    return stamp.strftime("%Y%m%d%H%M%S")


def planned_mirror(source: Path) -> tuple[str, str]:
    """(target filename, content) that `source` should produce under supabase/migrations/."""
    match = _FILENAME_RE.match(source.name)
    if not match:
        raise ValueError(f"{source.name} doesn't match the NNNN_name.sql convention")
    sequence, name = int(match.group(1)), match.group(2)
    target_name = f"{_timestamp_for(sequence)}_{name}.sql"
    return target_name, source.read_text(encoding="utf-8")


def planned_mirrors(source_dir: Path = SOURCE_DIR) -> dict[str, str]:
    return dict(planned_mirror(p) for p in sorted(source_dir.glob("*.sql")))


def sync(target_dir: Path = TARGET_DIR, source_dir: Path = SOURCE_DIR) -> list[str]:
    """Write the mirror files and remove any stale ones. Returns filenames written."""
    target_dir.mkdir(parents=True, exist_ok=True)
    planned = planned_mirrors(source_dir)

    for stale in target_dir.glob("*.sql"):
        if stale.name not in planned:
            stale.unlink()

    for name, content in planned.items():
        (target_dir / name).write_text(content, encoding="utf-8")

    return sorted(planned)


def check(target_dir: Path = TARGET_DIR, source_dir: Path = SOURCE_DIR) -> list[str]:
    """Problems found comparing the committed mirror against migrations/. Empty means in sync."""
    planned = planned_mirrors(source_dir)
    existing = (
        {p.name: p.read_text(encoding="utf-8") for p in target_dir.glob("*.sql")}
        if target_dir.exists()
        else {}
    )

    problems: list[str] = []
    for name, content in planned.items():
        if name not in existing:
            problems.append(f"missing: supabase/migrations/{name}")
        elif existing[name] != content:
            problems.append(
                f"stale (content differs from migrations/ source): supabase/migrations/{name}"
            )
    for name in existing:
        if name not in planned:
            problems.append(
                f"stale (no longer produced by migrations/): supabase/migrations/{name}"
            )

    return problems


def main() -> int:
    if "--check" in sys.argv:
        problems = check()
        if problems:
            print("supabase/migrations/ is out of sync with migrations/:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print("\nRun: python scripts/sync_supabase_migrations.py", file=sys.stderr)
            return 1
        print("supabase/migrations/ is in sync with migrations/.")
        return 0

    written = sync()
    print(f"Wrote {len(written)} file(s) to supabase/migrations/:")
    for name in written:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
