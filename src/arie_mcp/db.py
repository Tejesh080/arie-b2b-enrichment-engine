"""The read-only database access layer.

Connects only as ``arie_mcp_readonly`` (``Settings.readonly_database_url``),
only against ``mcp_diag`` objects (``migrations/0039_mcp_diag_schema.sql``).
No tool in this package ever runs a query outside that module — this is the
one place a SQL string is allowed to be built at all, and every string here
is a fixed, reviewed statement against a named ``mcp_diag`` view; nothing
here accepts caller-supplied SQL or identifiers.

**Timeout, defense in depth (spec Decision 4).** The role carries a
role-level ``statement_timeout`` default (migrations/0039), but this
project's Supabase pooler's handling of that default could not be verified
empirically from this environment. So every connection this module hands
out also issues an explicit, unconditional ``SET statement_timeout`` before
running anything — belt AND suspenders, not one or the other.

The pool is constructed with ``open=False`` and opened in the background
(``wait=False``), deliberately unlike ``arie.api.main``'s own pool
construction (``open=True``): this server must start successfully even when
the database is completely unreachable (spec § 14), and a blocking open at
construction would defeat that.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from arie_mcp.errors import DbUnavailableError, TimeoutToolError

_LOGGER = logging.getLogger("arie_mcp.db")

_POOL_ACQUIRE_TIMEOUT_S = 3.0
"""How long to wait for a connection to become available before reporting
DB_UNAVAILABLE. Separate from, and shorter than, the statement timeout —
this bounds "can't even get a connection", not "the query itself is slow"."""


def build_pool(dsn: str) -> ConnectionPool:
    pool = ConnectionPool(dsn, min_size=0, max_size=4, open=False)
    pool.open(wait=False)
    return pool


def run_query(
    pool: ConnectionPool,
    sql: str,
    params: dict[str, Any] | Sequence[Any] | None,
    *,
    statement_timeout_ms: int,
) -> list[dict[str, Any]]:
    """Run one read-only query and return its rows as plain dicts.

    Every failure mode maps to a typed error (``arie_mcp.errors``) — a
    caller never sees a raw psycopg exception or a connection string.
    """
    try:
        with pool.connection(timeout=_POOL_ACQUIRE_TIMEOUT_S) as conn:
            # See module docstring: this SET is unconditional, not
            # contingent on whatever the role-level default happens to do
            # under this environment's pooler. `statement_timeout_ms` is an
            # internal int (never caller-supplied), so building this string
            # directly is safe — nothing here is derived from tool input.
            conn.execute(f"SET statement_timeout = '{int(statement_timeout_ms)}ms'")
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
    except PoolTimeout as exc:
        raise DbUnavailableError(
            "the read-only diagnostic database is unavailable (pool timeout)"
        ) from exc
    except psycopg.errors.QueryCanceled as exc:
        raise TimeoutToolError(
            "a diagnostic query exceeded the database statement timeout"
        ) from exc
    except psycopg.OperationalError as exc:
        raise DbUnavailableError("the read-only diagnostic database is unavailable") from exc


def check_reachable(pool: ConnectionPool) -> bool:
    try:
        run_query(pool, "SELECT 1", None, statement_timeout_ms=1000)
        return True
    except DbUnavailableError:
        return False
    except TimeoutToolError:
        return False
