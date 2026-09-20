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

from tests.mcp.conftest import SeededJob, SeededScenario

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


async def test_extended_investigation_workflow_covers_all_seven_diagnostic_steps(
    mcp_readonly_database_url: str, audit_log_dir: str, seeded_scenario: SeededScenario
) -> None:
    """The fuller investigation shape slice 2 adds: after finding and
    inspecting a failing job, an agent also asks whether the *provider* it
    depended on is healthy, what it's been costing, and why ARIE's own
    acquisition policy made the choices it did for that lead. Each step's
    assertion checks the seeded scenario's actual data, not a plausible-
    looking shape.
    """
    params = _server_params(mcp_readonly_database_url, audit_log_dir)
    job = seeded_scenario.job

    async with Client(server=params) as client:
        # 1. get_system_health
        health = (await client.call_tool("get_system_health", {})).structured_content
        assert health["data"]["database_reachable"] is True
        assert health["data"]["queue"]["dead_letter"] >= 1

        # 2. get_recent_errors — the provider error this scenario seeded
        # should show up in the provider-errors-by-kind breakdown.
        recent = (
            await client.call_tool("get_recent_errors", {"window_minutes": 1440})
        ).structured_content
        assert recent["data"]["provider_error_count"] >= 1
        kinds = {row["key"] for row in recent["data"]["provider_errors_by_kind"]}
        assert "quota_exhausted" in kinds

        # 3. list_failed_jobs — the seeded job is in it.
        listed = (
            await client.call_tool("list_failed_jobs", {"status": "dead_letter", "limit": 100})
        ).structured_content
        job_ids = {j["job_id"] for j in listed["data"]["jobs"]}
        assert str(job.job_id) in job_ids

        # 4. inspect_job — the same job's own detail.
        detail = (
            await client.call_tool("inspect_job", {"job_id": str(job.job_id)})
        ).structured_content["data"]["job"]
        assert detail["status"] == "dead_letter"
        assert detail["lead_id"] == str(job.lead_id)

        # 5. inspect_provider_health — the provider this scenario's error
        # belongs to should now show a quota-cooldown signal.
        health_report = (
            await client.call_tool(
                "inspect_provider_health", {"provider": seeded_scenario.provider}
            )
        ).structured_content
        assert health_report["ok"] is True
        assert health_report["data"]["last_quota_error_at"] is not None

        # 6. get_enrichment_costs — the seeded provider_calls row (cost_usd
        # 0.0, an error call) is included in that provider's rollup.
        costs_report = (
            await client.call_tool("get_enrichment_costs", {"provider": seeded_scenario.provider})
        ).structured_content
        assert costs_report["ok"] is True
        rollup_providers = {row["group_key"] for row in costs_report["data"]["rows"]}
        assert seeded_scenario.provider in rollup_providers

        # 7. inspect_routing_decision — the voi_decisions step this
        # scenario seeded for the same lead.
        routing = (
            await client.call_tool("inspect_routing_decision", {"lead_id": str(job.lead_id)})
        ).structured_content
        assert routing["ok"] is True
        assert routing["data"]["lead_id"] == str(job.lead_id)
        assert routing["data"]["steps"][0]["candidate_provider"] == seeded_scenario.provider
        assert routing["data"]["steps"][0]["chosen"] is True

    audit_files = list(Path(audit_log_dir).glob("*.jsonl"))
    assert len(audit_files) == 1
    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    tools_called = [json.loads(line)["tool"] for line in lines]
    assert tools_called == [
        "get_system_health",
        "get_recent_errors",
        "list_failed_jobs",
        "inspect_job",
        "inspect_provider_health",
        "get_enrichment_costs",
        "inspect_routing_decision",
    ]
