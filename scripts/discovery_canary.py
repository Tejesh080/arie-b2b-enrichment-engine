"""Manual, one-off local canary for the full real Opportunity Activation
path: actual Firecrawl search, actual Firecrawl website verification, and
actual Hunter Domain Search buyer identification, feeding the orchestrator
(screening, promotion, scoring, selective research) against the local *test*
database only. `discovery_provider`/`website_verifier`/`buyer_search_fn` are
all left at their defaults, which are the real ones whenever the matching
credential is configured (`FIRECRAWL_API_KEY`, `HUNTER_API_KEY`).

Deliberately small (`max_candidates`/`requested_opportunity_count` below) —
Opportunity Activation Parts 22/23 ask for roughly 5 real website
verifications and at most 3 real buyer searches, not a bulk run.

Not part of the pytest suite — it prints a funnel report for a human to read
and cleans up everything it created. Run with TEST_DATABASE_URL /
ARIE_ALLOW_INTEGRATION_TEST_DB set, exactly like the integration suite:

    export TEST_DATABASE_URL=postgresql://arie:arie_local_dev@localhost:5432/arie_test
    export ARIE_ALLOW_INTEGRATION_TEST_DB=1
    python scripts/discovery_canary.py
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from psycopg_pool import ConnectionPool

from arie.config import IntegrationDatabaseConfig
from arie.discovery import repository
from arie.discovery.orchestrator import run_discovery
from arie.discovery.website_verification import VerificationResult, verify_candidate
from arie.icp_profiles import get_active_profile
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger
from arie.llm.fake_provider import FakeLLMProvider
from arie.llm.provider import LLMMessage
from arie.llm.service import LLMService
from arie.tenancy import LEGACY_ORGANIZATION_ID
from scripts.test_db import assert_not_production, marker_present

_CANDIDATE_ID_RE = re.compile(r"id: ([0-9a-fA-F-]{36})")


def _handler(messages: Sequence[LLMMessage]) -> str:
    """Deterministic search-plan/screening answers — already proven by the
    discovery-only canary. Website verification uses a *real* DeepSeek call
    instead (see `_real_website_verifier` below), so this handler is never
    asked for that schema."""
    rendered = "\n".join(m.content for m in messages)
    ids = _CANDIDATE_ID_RE.findall(rendered)
    if not ids:
        return json.dumps(
            {
                "queries": [
                    {"query": "Australian supplement distributors", "rationale": "canary"},
                    {"query": "sports nutrition retailers Australia", "rationale": "canary"},
                ]
            }
        )
    return json.dumps(
        {
            "results": [
                {
                    "candidate_id": i,
                    "screening_class": "promising",
                    "short_reason": "Real search result for a supplement distributor.",
                    "matching_traits": [],
                }
                for i in ids
            ]
        }
    )


def main() -> int:
    db = IntegrationDatabaseConfig()
    if not db.configured or not db.allow:
        print("TEST_DATABASE_URL / ARIE_ALLOW_INTEGRATION_TEST_DB not set — see module docstring.")
        return 1
    assert_not_production(db.url)
    if not marker_present(db.direct_url):
        print("target database is not designated for testing — run scripts/test_db.py designate")
        return 1

    pool = ConnectionPool(db.url, min_size=1, max_size=8, open=True)
    resolver = IdentityResolver(pool)
    queue = PostgresJobQueue(pool)
    ledger = PostgresCostLedger(pool)
    # Fake for search-plan/screening (deterministic, already proven) — a
    # *separate* real LLMService (DEEPSEEK_API_KEY is configured) for
    # website-verification extraction only, so this canary actually proves
    # real page text produces a sensible structured read, not a canned one.
    llm = LLMService(pool, ledger=ledger, provider=FakeLLMProvider(handler=_handler))
    real_llm = LLMService(pool, ledger=ledger)

    def _real_website_verifier(
        llm: LLMService | None,
        *,
        organization_id: UUID,
        domain: str,
        target_summary: str,
        now: datetime,
    ) -> VerificationResult:
        return verify_candidate(
            real_llm,
            organization_id=organization_id,
            domain=domain,
            target_summary=target_summary,
            now=now,
        )

    with pool.connection() as conn:
        profile = get_active_profile(conn, organization_id=LEGACY_ORGANIZATION_ID)

    run, opportunities = run_discovery(
        pool,
        resolver=resolver,
        queue=queue,
        ledger=ledger,
        llm=llm,
        organization_id=LEGACY_ORGANIZATION_ID,
        profile=profile,
        requested_opportunity_count=6,
        market="Australia",
        max_candidates=8,
        created_by_user_id=None,
        now=datetime.now(UTC),
        website_verifier=_real_website_verifier,
        # buyer_search_fn left at its default — real Hunter Domain Search
        # whenever HUNTER_API_KEY is configured.
    )

    print(f"run status: {run.status}")
    print(f"error: {run.error_detail}")
    print("funnel:")
    for key, value in run.funnel.as_dict().items():
        print(f"  {key}: {value}")
    print(f"opportunities: {len(opportunities)}")
    for o in opportunities:
        print(
            f"  - {o.company_name} ({o.domain}) [{o.priority}] next={o.next_action} "
            f"score={o.score} verification={o.verification_status}"
        )
        if o.buyer is not None and o.buyer.name_known:
            print(
                f"      buyer: {o.buyer.full_name} — {o.buyer.title} "
                f"| email={o.buyer.email} ({o.buyer.email_status})"
            )
        elif o.buyer is not None:
            print(f"      buyer: role signal only (seniority={o.buyer.seniority}, no name)")
        else:
            print("      buyer: not identified")

    with pool.connection() as conn:
        candidates = repository.list_candidates(
            conn, run_id=run.run_id, organization_id=LEGACY_ORGANIZATION_ID
        )
        lead_ids = [c.promoted_lead_id for c in candidates if c.promoted_lead_id is not None]
        with conn.cursor() as cur:
            if lead_ids:
                cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
            cur.execute("DELETE FROM discovery_runs WHERE run_id = %s", (run.run_id,))
        conn.commit()
    print("cleaned up.")
    pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
