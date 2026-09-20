"""Proves the DB statement timeout actually fires against a real Postgres,
at both layers spec Decision 4 asks for: the role-level default
(migrations/0039's ``ALTER ROLE ... SET statement_timeout``) and the
explicit per-connection ``SET`` (``arie_mcp.db.run_query``, defense in
depth in case a pooler doesn't preserve the role-level default).
"""

from __future__ import annotations

import time

import pytest

from arie_mcp import db
from arie_mcp.errors import TimeoutToolError

pytestmark = pytest.mark.integration


async def test_slow_query_is_cut_off_by_the_role_level_default(
    mcp_readonly_database_url: str,
) -> None:
    """No explicit ``statement_timeout_ms`` override here — this is
    ``run_query``'s normal path, which still sets it explicitly per spec
    Decision 4's defense-in-depth, but at the same 5000ms the role-level
    default (migrations/0039) also carries, so this proves both layers
    agree, not just that the Python-side override alone works."""
    pool = db.build_pool(mcp_readonly_database_url)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutToolError):
            db.run_query(pool, "SELECT pg_sleep(10)", None, statement_timeout_ms=5000)
    finally:
        pool.close()
    elapsed = time.monotonic() - started
    # Cut off near 5s, not left to run the full pg_sleep(10).
    assert elapsed < 8.0


async def test_shorter_explicit_timeout_overrides_and_fires_sooner(
    mcp_readonly_database_url: str,
) -> None:
    """The explicit per-connection SET (spec Decision 4) is what actually
    governs, independent of the 5000ms role-level default — a 1s override
    cuts off a 10s sleep in ~1s, not ~5s."""
    pool = db.build_pool(mcp_readonly_database_url)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutToolError):
            db.run_query(pool, "SELECT pg_sleep(10)", None, statement_timeout_ms=1000)
    finally:
        pool.close()
    elapsed = time.monotonic() - started
    assert elapsed < 4.0


async def test_fast_query_is_unaffected_by_the_timeout(mcp_readonly_database_url: str) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        rows = db.run_query(pool, "SELECT 1 AS one", None, statement_timeout_ms=5000)
    finally:
        pool.close()
    assert rows == [{"one": 1}]
