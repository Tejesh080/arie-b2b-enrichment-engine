"""The Claude-Code-shaped debugging workflow (spec § 16, § 17): seed a
dead-lettered job into the disposable database, then drive
``get_system_health`` -> ``list_failed_jobs`` -> ``inspect_job`` through
the actual MCP stdio transport, in sequence, exactly as an agent debugging
"why did this job fail" would. Every assertion checks the *seeded state* is
actually what the chained calls report — not narrated, not fabricated.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

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
        command=sys.executable, args=["-m", "arie_mcp.server"], env=env, cwd=os.getcwd()
    )


async def test_debugging_workflow_finds_and_explains_a_seeded_failure(
    mcp_readonly_database_url: str, audit_log_dir: str, seeded_dead_letter_job: SeededJob
) -> None:
    params = _server_params(mcp_readonly_database_url, audit_log_dir)

    async with Client(server=params) as client:
        # Step 1 — is the system healthy at all? A real agent starts here
        # before assuming a specific job is the problem.
        health_result = await client.call_tool("get_system_health", {})
        assert health_result.is_error is False
        health = health_result.structured_content
        assert health["ok"] is True
        assert health["data"]["database_reachable"] is True
        assert health["data"]["queue"]["dead_letter"] >= 1

        # Step 2 — what's failed? List dead-lettered jobs and confirm the
        # seeded one is actually among them (by job_id, not just a count).
        list_result = await client.call_tool(
            "list_failed_jobs", {"status": "dead_letter", "limit": 100}
        )
        assert list_result.is_error is False
        listed = list_result.structured_content
        assert listed["ok"] is True
        job_ids = {job["job_id"] for job in listed["data"]["jobs"]}
        assert str(seeded_dead_letter_job.job_id) in job_ids

        # Step 3 — inspect that specific job for the detail a human/agent
        # would act on: its error, attempt count, and parent lead's status.
        detail_result = await client.call_tool(
            "inspect_job", {"job_id": str(seeded_dead_letter_job.job_id)}
        )
        assert detail_result.is_error is False
        detail = detail_result.structured_content["data"]["job"]
        assert detail["job_id"] == str(seeded_dead_letter_job.job_id)
        assert detail["status"] == "dead_letter"
        assert detail["attempt_count"] == 3
        assert detail["last_error"] == "RuntimeError: synthetic failure for tests/mcp"
        assert detail["lead_id"] == str(seeded_dead_letter_job.lead_id)
        assert detail["lead_status"] == "FAILED"

    # Step 4 — every call in the chain left an audit trail, correlated by
    # its own id, findable after the fact (spec § 12).
    audit_files = list(Path(audit_log_dir).glob("*.jsonl"))
    assert len(audit_files) == 1
    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    tools_called = [json.loads(line)["tool"] for line in lines]
    assert tools_called == ["get_system_health", "list_failed_jobs", "inspect_job"]
