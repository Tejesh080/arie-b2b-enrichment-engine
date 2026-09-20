"""The single response envelope every tool returns (spec § 9), and the one
place that builds it.

``run_tool`` is the only caller of a tool implementation's body. It applies
the wall-clock timeout, converts every :class:`~arie_mcp.errors.ToolError`
and every unexpected exception into a typed, non-leaking ``ToolResult``, and
writes exactly one audit record per invocation — success or failure. A tool
module never constructs a failing ``ToolResult`` itself; it raises a
``ToolError`` subclass and lets this module decide what the caller sees.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from arie_mcp import __version__
from arie_mcp.audit import AuditLogger, AuditRecord
from arie_mcp.errors import ErrorCode, ToolError
from arie_mcp.settings import Settings

_LOGGER = logging.getLogger("arie_mcp")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


class ToolResult(BaseModel):
    """The envelope every tool call returns, success or failure alike —
    never a bare exception, never an untyped shape."""

    ok: bool
    data: dict[str, Any] | None = None
    error_code: ErrorCode | None = None
    message: str | None = None
    truncated: bool = False
    row_count: int | None = None
    correlation_id: str


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool implementation body returns on success — ``run_tool``
    wraps this into a ``ToolResult``."""

    data: dict[str, Any]
    row_count: int | None = None
    truncated: bool = False


async def run_tool(
    tool_name: str,
    body: Callable[[], Awaitable[ToolOutcome]],
    *,
    settings: Settings,
    audit_logger: AuditLogger,
    input_payload: dict[str, Any],
) -> ToolResult:
    correlation_id = new_correlation_id()
    started = time.monotonic()

    error_code: ErrorCode | None = None
    message: str | None = None
    data: dict[str, Any] | None = None
    row_count: int | None = None
    truncated = False
    status: Literal["ok", "error", "timeout"] = "ok"

    try:
        outcome = await asyncio.wait_for(body(), timeout=settings.tool_timeout_seconds)
        data = outcome.data
        row_count = outcome.row_count
        truncated = outcome.truncated
    except TimeoutError:
        # asyncio.TimeoutError is `TimeoutError` since Python 3.11 — this
        # also catches the outer wall-clock ceiling firing around a body
        # that never raised its own TimeoutToolError (e.g. a hung HTTP call).
        status = "timeout"
        error_code = ErrorCode.TIMEOUT
        message = f"{tool_name} exceeded its {settings.tool_timeout_seconds:.0f}s timeout."
    except ToolError as exc:
        status = "timeout" if exc.error_code == ErrorCode.TIMEOUT else "error"
        error_code = exc.error_code
        message = exc.message
    except Exception:
        # Deliberately never `str(exc)` here — an unclassified exception may
        # carry a connection string or other detail that must never reach
        # the caller. Full detail is logged locally, keyed by correlation_id,
        # exactly what an operator needs and nothing a caller shouldn't have.
        _LOGGER.exception(
            "unhandled exception in tool %s (correlation_id=%s)", tool_name, correlation_id
        )
        status = "error"
        error_code = ErrorCode.INTERNAL
        message = (
            "An unexpected internal error occurred. Details were logged "
            f"locally under correlation_id={correlation_id}."
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    result = ToolResult(
        ok=error_code is None,
        data=data,
        error_code=error_code,
        message=message,
        truncated=truncated,
        row_count=row_count,
        correlation_id=correlation_id,
    )

    audit_logger.write(
        AuditRecord(
            ts=datetime.now(UTC),
            correlation_id=correlation_id,
            tool=tool_name,
            input=input_payload,
            outcome=status,
            error_code=error_code,
            duration_ms=duration_ms,
            row_count=row_count,
            truncated=truncated,
            caller_pid=os.getpid(),
            mcp_server_version=__version__,
        )
    )

    return result
