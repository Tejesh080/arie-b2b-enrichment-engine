"""``inspect_migrations`` (spec § 9.3) — applied/pending/checksum state
only. No migration-apply capability exists anywhere in this package;
``scripts/migrate.py --apply`` is never invoked by ``arie_mcp``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from arie.migrations import MigrationsDirectoryError, checksum_of, migration_files
from arie_mcp import db
from arie_mcp.envelope import ToolOutcome
from arie_mcp.errors import DbUnavailableError, InternalToolError


class MigrationStatus(BaseModel):
    filename: str
    applied: bool
    applied_at: datetime | None
    checksum_matches: bool | None
    """``None`` when not yet applied — there is nothing on record to compare
    the on-disk file against."""


class MigrationsReport(BaseModel):
    directory_count: int
    applied_count: int
    pending: list[str]
    migrations: list[MigrationStatus]


def _collect(pool: ConnectionPool, *, statement_timeout_ms: int) -> MigrationsReport:
    try:
        on_disk = migration_files()
    except MigrationsDirectoryError as exc:
        raise InternalToolError("could not resolve the migrations directory") from exc

    rows = db.run_query(
        pool,
        "SELECT filename, checksum, applied_at FROM mcp_diag.v_schema_migrations",
        None,
        statement_timeout_ms=statement_timeout_ms,
    )
    applied_by_name: dict[str, dict[str, Any]] = {row["filename"]: row for row in rows}

    statuses: list[MigrationStatus] = []
    pending: list[str] = []
    for path in on_disk:
        name = path.name
        applied_row = applied_by_name.get(name)
        if applied_row is None:
            pending.append(name)
            statuses.append(
                MigrationStatus(
                    filename=name, applied=False, applied_at=None, checksum_matches=None
                )
            )
            continue
        # Same read/checksum pattern scripts/migrate.py itself uses
        # (path.read_text(encoding="utf-8") -> checksum_of) — comparing
        # against a differently-computed checksum here would produce a
        # spurious mismatch that says nothing true about the file.
        on_disk_checksum = checksum_of(path.read_text(encoding="utf-8"))
        statuses.append(
            MigrationStatus(
                filename=name,
                applied=True,
                applied_at=applied_row["applied_at"],
                checksum_matches=on_disk_checksum == applied_row["checksum"],
            )
        )

    return MigrationsReport(
        directory_count=len(on_disk),
        applied_count=len(applied_by_name),
        pending=pending,
        migrations=statuses,
    )


async def inspect_migrations_impl(
    pool: ConnectionPool | None, *, statement_timeout_ms: int
) -> ToolOutcome:
    if pool is None:
        raise DbUnavailableError("MCP_READONLY_DATABASE_URL is not configured")

    report = await asyncio.to_thread(_collect, pool, statement_timeout_ms=statement_timeout_ms)
    return ToolOutcome(data=report.model_dump(mode="json"), row_count=report.directory_count)
