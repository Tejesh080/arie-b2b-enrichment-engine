from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from psycopg_pool import ConnectionPool

from arie.migrations import checksum_of
from arie_mcp import db
from arie_mcp.errors import DbUnavailableError
from arie_mcp.tools import migrations


def _fake_pool() -> ConnectionPool:
    return cast(ConnectionPool, object())


class _FakePath:
    def __init__(self, name: str, content: str = "SELECT 1;") -> None:
        self.name = name
        self._content = content

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._content


async def test_no_pool_raises_db_unavailable() -> None:
    with pytest.raises(DbUnavailableError):
        await migrations.inspect_migrations_impl(None, statement_timeout_ms=5000)


async def test_reports_applied_and_pending_with_checksum_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied_content = "CREATE TABLE foo();"
    matching_checksum = checksum_of(applied_content)

    monkeypatch.setattr(
        migrations,
        "migration_files",
        lambda: [
            _FakePath("0001_init.sql", applied_content),
            _FakePath("0002_new.sql", "CREATE TABLE bar();"),
        ],
    )

    def fake_run_query(
        pool: object, sql: str, params: object, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [
            {
                "filename": "0001_init.sql",
                "checksum": matching_checksum,
                "applied_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await migrations.inspect_migrations_impl(_fake_pool(), statement_timeout_ms=5000)

    assert outcome.data["directory_count"] == 2
    assert outcome.data["applied_count"] == 1
    assert outcome.data["pending"] == ["0002_new.sql"]
    by_name = {m["filename"]: m for m in outcome.data["migrations"]}
    assert by_name["0001_init.sql"]["applied"] is True
    assert by_name["0001_init.sql"]["checksum_matches"] is True
    assert by_name["0002_new.sql"]["applied"] is False
    assert by_name["0002_new.sql"]["checksum_matches"] is None


async def test_flags_checksum_mismatch_for_an_edited_applied_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        migrations,
        "migration_files",
        lambda: [_FakePath("0001_init.sql", "CREATE TABLE foo_edited();")],
    )

    def fake_run_query(
        pool: object, sql: str, params: object, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [
            {
                "filename": "0001_init.sql",
                "checksum": "not-the-real-checksum",
                "applied_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        ]

    monkeypatch.setattr(db, "run_query", fake_run_query)

    outcome = await migrations.inspect_migrations_impl(_fake_pool(), statement_timeout_ms=5000)

    assert outcome.data["migrations"][0]["checksum_matches"] is False
