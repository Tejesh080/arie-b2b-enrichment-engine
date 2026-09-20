"""Structured JSONL audit logging (spec § 12).

Host-local only in V0.1 (spec Decision 6): one append-only file per UTC day
under ``Settings.audit_log_dir``, never routed through the read-only DB
role, never requiring a Docker volume. ``input`` is logged verbatim because
every V0.1 tool's input is an ID/enum/bounded scalar by construction — no
tool in this slice accepts anything secret-shaped; a future tool whose input
could contain something sensitive must redact before an ``AuditRecord`` is
built, not after.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from arie_mcp.errors import ErrorCode

_LOGGER = logging.getLogger("arie_mcp.audit")


class AuditRecord(BaseModel):
    ts: datetime
    correlation_id: str
    tool: str
    input: dict[str, Any]
    outcome: str  # "ok" | "error" | "timeout"
    error_code: ErrorCode | None = None
    duration_ms: int
    row_count: int | None = None
    truncated: bool = False
    caller_pid: int
    mcp_server_version: str


class AuditLogger:
    """Appends one JSON line per tool invocation to
    ``<audit_log_dir>/arie-mcp-YYYY-MM-DD.jsonl`` (UTC date).

    A write failure here must never fail the tool call it's auditing for —
    the same "observability failure is not a processing failure" discipline
    ``arie.jobs.heartbeat.beat`` already follows for its own best-effort
    write. A lock serializes writes from concurrent tool calls onto one
    file handle; local JSONL volume from a single stdio client never
    justifies more than that.
    """

    def __init__(self, log_dir: str) -> None:
        self._log_dir = Path(log_dir)
        self._lock = threading.Lock()

    def _path_for(self, ts: datetime) -> Path:
        return self._log_dir / f"arie-mcp-{ts.date().isoformat()}.jsonl"

    def write(self, record: AuditRecord) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            line = record.model_dump_json()
            with self._lock, self._path_for(record.ts).open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
                fh.flush()
        except Exception:
            _LOGGER.exception(
                "failed to write MCP audit record (correlation_id=%s, tool=%s)",
                record.correlation_id,
                record.tool,
            )
