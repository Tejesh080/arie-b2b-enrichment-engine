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

import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
from arie.tenancy import LEGACY_ORGANIZATION_ID
from tests.mcp.conftest import SeededJob

pytestmark = pytest.mark.integration

_ALL_V0_1_TOOLS = {
    "get_system_health",
    "inspect_job",
    "list_failed_jobs",
    "inspect_api_contract",
    "inspect_migrations",
    "get_recent_errors",
    "list_providers",
    "inspect_provider_health",
    "get_enrichment_costs",
    "inspect_routing_decision",
    "inspect_configuration",
}


def _server_params(
    mcp_readonly_database_url: str, audit_log_dir: str, *, api_base_url: str | None = None
) -> StdioServerParameters:
    env = dict(os.environ)
    env["MCP_READONLY_DATABASE_URL"] = mcp_readonly_database_url
    env["ARIE_MCP_AUDIT_LOG_PATH"] = audit_log_dir
    if api_base_url is not None:
        env["ARIE_MCP_API_BASE_URL"] = api_base_url
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "arie_mcp.server"],
        env=env,
        cwd=os.getcwd(),
    )


_FAKE_OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "fake ARIE API for tests/mcp/integration"},
    "paths": {
        "/healthz": {"get": {"summary": "Health"}},
        "/leads": {"post": {"summary": "Ingest a lead"}},
    },
    "components": {"schemas": {}},
}


@pytest.fixture
def fake_api_server() -> Iterator[str]:
    """A minimal, real HTTP server on localhost serving a fixed
    ``/openapi.json`` — used instead of depending on this repo's actual API
    process being up (portable across CI and any developer machine),
    so ``inspect_api_contract`` still gets a genuine subprocess-to-HTTP
    round trip, not a mock swapped in-process."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/openapi.json":
                body = json.dumps(_FAKE_OPENAPI_SPEC).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep test output quiet

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_server_advertises_exactly_the_eleven_v0_1_tools(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.list_tools()
    names = {tool.name for tool in result.tools}
    assert names == _ALL_V0_1_TOOLS


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


async def test_inspect_migrations_round_trip(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_migrations", {})
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["pending"] == []
    assert payload["data"]["applied_count"] > 0


async def test_get_recent_errors_round_trip(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("get_recent_errors", {"window_minutes": 1440})
    payload = result.structured_content
    assert payload["ok"] is True
    assert "job_failure_count" in payload["data"]


async def test_get_recent_errors_round_trip_rejects_window_above_1440(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("get_recent_errors", {"window_minutes": 100_000})
    assert result.is_error is True


async def test_list_providers_round_trip(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("list_providers", {})
    payload = result.structured_content
    assert payload["ok"] is True
    names = [p["name"] for p in payload["data"]["providers"]]
    assert REGISTERED_LIVE_PROVIDER_NAMES[0] in names


async def test_inspect_provider_health_round_trip(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool(
            "inspect_provider_health", {"provider": REGISTERED_LIVE_PROVIDER_NAMES[0]}
        )
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["scope"] == "global"


async def test_inspect_provider_health_round_trip_rejects_unknown_provider(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_provider_health", {"provider": "not_real"})
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["error_code"] == "INVALID_INPUT"


async def test_get_enrichment_costs_round_trip(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("get_enrichment_costs", {})
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["group_by"] == "provider"


async def test_inspect_routing_decision_round_trip_not_found(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool(
            "inspect_routing_decision", {"lead_id": "00000000-0000-0000-0000-000000000000"}
        )
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["error_code"] == "NOT_FOUND"


async def test_inspect_configuration_round_trip_process_only(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_configuration", {})
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["organization"] is None
    assert isinstance(payload["data"]["process"]["categories"], dict)


async def test_inspect_configuration_round_trip_with_organization(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    async with Client(server=params) as client:
        result = await client.call_tool(
            "inspect_configuration", {"organization_id": str(LEGACY_ORGANIZATION_ID)}
        )
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["organization"]["organization_id"] == str(LEGACY_ORGANIZATION_ID)
    assert "entitlements" in payload["data"]["organization"]
    blob = str(payload)
    assert "stripe_" not in blob


async def test_inspect_api_contract_round_trip_against_a_real_http_server(
    mcp_readonly_database_url: str, audit_log_dir: str, fake_api_server: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir, api_base_url=fake_api_server)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_api_contract", {})
    payload = result.structured_content
    assert payload["ok"] is True
    paths = {r["path"] for r in payload["data"]["routes"]}
    assert paths == {"/healthz", "/leads"}


async def test_inspect_api_contract_round_trip_path_prefix_filter(
    mcp_readonly_database_url: str, audit_log_dir: str, fake_api_server: str
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir, api_base_url=fake_api_server)
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_api_contract", {"path_prefix": "/leads"})
    payload = result.structured_content
    assert payload["ok"] is True
    assert payload["data"]["route_count"] == 1


async def test_inspect_api_contract_round_trip_upstream_unavailable(
    mcp_readonly_database_url: str, audit_log_dir: str
) -> None:
    """No fake server on this port — proves the tool reports
    UPSTREAM_UNAVAILABLE cleanly rather than hanging or crashing the
    server process."""
    params = _server_params(
        mcp_readonly_database_url, audit_log_dir, api_base_url="http://127.0.0.1:1"
    )
    async with Client(server=params) as client:
        result = await client.call_tool("inspect_api_contract", {})
    payload = result.structured_content
    assert payload["ok"] is False
    assert payload["error_code"] == "UPSTREAM_UNAVAILABLE"
