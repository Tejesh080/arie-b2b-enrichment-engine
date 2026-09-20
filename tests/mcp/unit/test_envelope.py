"""Unit tests for the single envelope every tool call passes through.
Uses a real ``AuditLogger`` against a tmp directory (fast, local file I/O —
not what the ``integration`` marker is for) rather than mocking it, so the
JSONL shape is verified for real.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from arie_mcp.audit import AuditLogger
from arie_mcp.envelope import ToolOutcome, run_tool
from arie_mcp.errors import ErrorCode, NotFoundError
from arie_mcp.settings import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base = {"audit_log_dir": str(tmp_path / "audit"), "tool_timeout_seconds": 10.0}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _read_audit_lines(tmp_path: Path) -> list[dict[str, object]]:
    audit_dir = tmp_path / "audit"
    files = list(audit_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


async def test_successful_tool_call_builds_ok_result_and_audit_record(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audit_logger = AuditLogger(settings.audit_log_dir)

    async def body() -> ToolOutcome:
        return ToolOutcome(data={"hello": "world"}, row_count=1, truncated=False)

    result = await run_tool(
        "sample_tool", body, settings=settings, audit_logger=audit_logger, input_payload={"x": 1}
    )

    assert result.ok is True
    assert result.data == {"hello": "world"}
    assert result.error_code is None
    assert result.row_count == 1
    assert result.correlation_id

    records = _read_audit_lines(tmp_path)
    assert len(records) == 1
    assert records[0]["tool"] == "sample_tool"
    assert records[0]["outcome"] == "ok"
    assert records[0]["correlation_id"] == result.correlation_id
    assert records[0]["input"] == {"x": 1}


async def test_tool_error_becomes_typed_result_with_its_own_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audit_logger = AuditLogger(settings.audit_log_dir)

    async def body() -> ToolOutcome:
        raise NotFoundError("no job found with job_id=deadbeef")

    result = await run_tool(
        "inspect_job", body, settings=settings, audit_logger=audit_logger, input_payload={}
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.NOT_FOUND
    assert result.message == "no job found with job_id=deadbeef"

    records = _read_audit_lines(tmp_path)
    assert records[0]["outcome"] == "error"
    assert records[0]["error_code"] == "NOT_FOUND"


async def test_unhandled_exception_never_leaks_its_raw_message(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    audit_logger = AuditLogger(settings.audit_log_dir)
    secret_bearing_message = "connection failed: postgresql://user:hunter2@db.internal/prod"

    async def body() -> ToolOutcome:
        raise RuntimeError(secret_bearing_message)

    result = await run_tool(
        "get_system_health", body, settings=settings, audit_logger=audit_logger, input_payload={}
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.INTERNAL
    assert result.message is not None
    assert "hunter2" not in result.message
    assert "postgresql://" not in result.message
    assert result.correlation_id in result.message  # the operator's lookup key


async def test_slow_tool_body_is_cut_off_at_the_wall_clock_timeout(tmp_path: Path) -> None:
    settings = _settings(tmp_path, tool_timeout_seconds=0.05)
    audit_logger = AuditLogger(settings.audit_log_dir)

    async def body() -> ToolOutcome:
        await asyncio.sleep(5)
        return ToolOutcome(data={})  # pragma: no cover - never reached

    result = await run_tool(
        "slow_tool", body, settings=settings, audit_logger=audit_logger, input_payload={}
    )

    assert result.ok is False
    assert result.error_code == ErrorCode.TIMEOUT

    records = _read_audit_lines(tmp_path)
    assert records[0]["outcome"] == "timeout"
