"""Black-box PII test (spec § 16): seeds a lead whose person/company carry
a real-shaped name, email, and domain, then asserts those exact strings
never appear anywhere in ``inspect_job``/``list_failed_jobs``'s serialized
output — a string search over the actual response, not an inspection of
which columns a view selects. This is the check that would catch a future
regression (someone widening ``mcp_diag.v_job_detail`` to join ``persons``)
that a column-level review might miss.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import psycopg
import pytest

from arie_mcp import db
from arie_mcp.limits import MAX_STRING_LEN
from arie_mcp.tools import jobs
from tests.mcp.conftest import delete_job_and_lead, seed_job

pytestmark = pytest.mark.integration


def _serialized(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


async def test_inspect_job_never_leaks_name_email_or_domain(
    mcp_readonly_database_url: str, seeded_pii_job: dict[str, str]
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await jobs.inspect_job_impl(
            pool, UUID(seeded_pii_job["job_id"]), statement_timeout_ms=5000
        )
    finally:
        pool.close()

    blob = _serialized(outcome.data)
    assert seeded_pii_job["full_name"] not in blob
    assert seeded_pii_job["email"] not in blob
    assert seeded_pii_job["domain"] not in blob


async def test_list_failed_jobs_never_leaks_name_email_or_domain(
    mcp_readonly_database_url: str, seeded_pii_job: dict[str, str]
) -> None:
    pool = db.build_pool(mcp_readonly_database_url)
    try:
        outcome = await jobs.list_failed_jobs_impl(
            pool, jobs.ListFailedJobsInput(limit=100), statement_timeout_ms=5000
        )
    finally:
        pool.close()

    blob = _serialized(outcome.data)
    assert seeded_pii_job["full_name"] not in blob
    assert seeded_pii_job["email"] not in blob
    assert seeded_pii_job["domain"] not in blob


async def test_last_error_is_length_capped_not_pii_redacted(
    mcp_readonly_database_url: str, admin_conn: psycopg.Connection[Any]
) -> None:
    """Documents a known, spec-accepted limitation (spec § 8) rather than
    leaving it implicit: ``last_error`` is truncated but not pattern-redacted
    — if application code ever puts a name or email into an exception
    message, it passes through (capped at ``MAX_STRING_LEN``) rather than
    being scrubbed. The two tests above prove the guarantee this project
    actually makes (no ``persons``/``companies`` join is reachable); this
    test proves the boundary of that guarantee is exactly where the spec
    says it is, not further.
    """
    seeded = seed_job(
        admin_conn,
        last_error="lookup failed for Jordan Ellis <jordan.ellis@example-corp.test> " + "x" * 600,
    )
    try:
        pool = db.build_pool(mcp_readonly_database_url)
        try:
            outcome = await jobs.inspect_job_impl(pool, seeded.job_id, statement_timeout_ms=5000)
        finally:
            pool.close()

        last_error = outcome.data["job"]["last_error"]
        assert len(last_error) == MAX_STRING_LEN
        # The name/email are still present within the capped string — this
        # is the documented limitation, asserted explicitly rather than
        # silently relied upon.
        assert "Jordan Ellis" in last_error
    finally:
        delete_job_and_lead(admin_conn, seeded)
