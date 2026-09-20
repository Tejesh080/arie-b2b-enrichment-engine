"""Real MCP stdio round-trip (spec § 16): spawns ``python -m
arie_mcp.server`` as an actual subprocess and drives it through the real
MCP client/session machinery — not a direct Python function call. This is
what proves the *server*, not just the tool implementations, works: tool
registration, JSON schema advertisement, request/response serialization,
and this repo's own ``ToolResult`` envelope surviving the wire.

Each test opens its own client (an ``async with Client(...)`` block inline,
not a shared fixture) — a fixture that yields across pytest-asyncio's
per-test task boundary tripped anyio's cancel-scope task matching during
teardown; opening and closing the subprocess within one task per test
avoids that entirely.
"""

from __future__ import annotations

import os
import sys

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from tests.mcp.conftest import SeededJob

pytestmark = pytest.mark.integration


def _server_params(mcp_readonly_database_url: str, audit_log_dir: str) -> StdioServerParameters:
    env = dict(os.environ)
    env["MCP_READONLY_DATABASE_URL"] = mcp_readonly_database_url
    env["ARIE_MCP_AUDIT_LOG_PATH"] = audit_log_dir
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "arie_mcp.server"],
        env=env,
        cwd=os.getcwd(),
    )


async def test_server_advertises_exactly_the_three_v0_1_tools(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert names == {"get_system_health", "inspect_job", "list_failed_jobs"}


async def test_every_tool_is_advertised_read_only(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.list_tools()
    for tool in result.tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_get_system_health_round_trip_reports_database_reachable(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("get_system_health", {})
    assert result.is_error is False
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["database_reachable"] is True
    assert "correlation_id" in payload


async def test_inspect_job_round_trip_not_found(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool(
            "inspect_job", {"job_id": "00000000-0000-0000-0000-000000000000"}
        )
    assert result.is_error is False
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["error_code"] == "NOT_FOUND"


async def test_inspect_job_round_trip_finds_a_seeded_job(
    mcp_readonly_database_url: str, audit_log_dir: str, seeded_dead_letter_job: SeededJob
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool(
            "inspect_job", {"job_id": str(seeded_dead_letter_job.job_id)}
        )
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["job"]["job_id"] == str(seeded_dead_letter_job.job_id)
    assert payload["data"]["job"]["status"] == "dead_letter"


async def test_malformed_job_id_is_rejected_by_the_protocol_schema(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_job", {"job_id": "not-a-uuid"})
    # Rejected before this codebase's own tool body ever runs — a JSON
    # Schema/pydantic validation failure at the MCP protocol layer, not a
    # NOT_FOUND from inside the tool. Surfaces as a protocol-level error,
    # never a raw traceback.
    assert result.is_error is True


async def test_list_failed_jobs_round_trip_rejects_limit_above_100(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("list_failed_jobs", {"limit": 101})
    assert result.is_error is True


async def test_list_failed_jobs_round_trip_default_call(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("list_failed_jobs", {})
    assert result.is_error is False
    payload = result.structured_content
    assert payload["ok"] is True
    assert "jobs" in payload["data"]
