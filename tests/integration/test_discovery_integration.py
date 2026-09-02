"""End-to-end discovery funnel: targeting profile -> search plan -> fake
discovery -> dedupe -> screening -> promotion -> the existing scoring
pipeline -> selective research -> opportunities.

Real Postgres (`migrated_database`), a fake discovery provider (no network),
and a scripted `FakeLLMProvider` standing in for the search-planning and
screening calls — the same "no DeepSeek key needed" contract every other M7
integration test already runs under.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

import arie.discovery.orchestrator as orchestrator_module
from arie.api.main import AppState, create_app, get_auth_context, get_llm_service
from arie.auth import AuthContext
from arie.discovery import repository
from arie.discovery.models import DiscoveryRunStatus
from arie.discovery.orchestrator import run_discovery
from arie.discovery.providers import DiscoveryProviderError, FakeDiscoveryProvider
from arie.icp_profiles import get_active_profile
from arie.identity.resolver import IdentityResolver
from arie.jobs.queue import PostgresJobQueue
from arie.ledger.store import PostgresCostLedger
from arie.llm.fake_provider import FakeLLMProvider
from arie.llm.provider import LLMMessage
from arie.llm.service import LLMService
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

_CANDIDATE_ID_RE = re.compile(r"id: ([0-9a-fA-F-]{36})")


def _classify_all_promising_handler(messages: Sequence[LLMMessage]) -> str:
    """A scripted screener: the first call is search planning (its own
    schema has no `id:` lines to match, so this returns a plan), every call
    after that is a screening batch — classify every submitted candidate_id
    as promising, so the funnel test exercises promotion and scoring."""
    rendered = "\n".join(m.content for m in messages)
    ids = _CANDIDATE_ID_RE.findall(rendered)
    if not ids:
        return json.dumps(
            {"queries": [{"query": "multi-location gyms Australia", "rationale": "test"}]}
        )
    return json.dumps(
        {
            "results": [
                {
                    "candidate_id": candidate_id,
                    "screening_class": "promising",
                    "short_reason": "Matches the target profile.",
                    "matching_traits": ["multi-location"],
                }
                for candidate_id in ids
            ]
        }
    )


@pytest.fixture
def discovery_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    pool = ConnectionPool(migrated_database, min_size=1, max_size=8, open=True)
    try:
        yield pool
    finally:
        pool.close()


@pytest.fixture
def discovery_cleanup(db_conn: psycopg.Connection) -> Iterator[list[uuid.UUID]]:
    run_ids: list[uuid.UUID] = []
    yield run_ids
    if not run_ids:
        return
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT promoted_lead_id FROM discovery_candidates "
            "WHERE run_id = ANY(%s) AND promoted_lead_id IS NOT NULL",
            (run_ids,),
        )
        lead_ids = [row[0] for row in cur.fetchall()]
        if lead_ids:
            cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
        # discovery_candidates cascades from discovery_runs.
        cur.execute("DELETE FROM discovery_runs WHERE run_id = ANY(%s)", (run_ids,))
    db_conn.commit()


def test_discovery_funnel_end_to_end(
    discovery_pool: ConnectionPool,
    job_queue: PostgresJobQueue,
    identity_resolver: IdentityResolver,
    discovery_cleanup: list[uuid.UUID],
) -> None:
    ledger = PostgresCostLedger(discovery_pool)
    llm = LLMService(
        discovery_pool,
        ledger=ledger,
        provider=FakeLLMProvider(handler=_classify_all_promising_handler),
    )

    with discovery_pool.connection() as conn:
        profile = get_active_profile(conn, organization_id=LEGACY_ORGANIZATION_ID)

    run, opportunities = run_discovery(
        discovery_pool,
        resolver=identity_resolver,
        queue=job_queue,
        ledger=ledger,
        llm=llm,
        organization_id=LEGACY_ORGANIZATION_ID,
        profile=profile,
        requested_opportunity_count=5,
        market="Australia",
        max_candidates=12,
        created_by_user_id=None,
        now=datetime.now(UTC),
        discovery_provider=FakeDiscoveryProvider(),
    )
    discovery_cleanup.append(run.run_id)

    assert run.status is DiscoveryRunStatus.COMPLETE
    assert run.error_detail is None

    funnel = run.funnel
    # A search plan ran, candidates were discovered and deduplicated, every
    # one was screened, and — because the scripted LLM classified them all
    # promising — every survivor was promoted into a real, scored lead.
    assert funnel.search_queries >= 1
    assert funnel.raw_candidates > 0
    assert funnel.unique_companies > 0
    assert funnel.screened == funnel.unique_companies
    assert funnel.promising == funnel.unique_companies
    assert funnel.unlikely == 0
    assert funnel.promoted_to_leads == funnel.unique_companies
    assert funnel.final_opportunities == min(5, funnel.unique_companies)
    assert funnel.llm_calls >= 2  # one search-plan call, at least one screening batch

    assert len(opportunities) == funnel.final_opportunities
    for opportunity in opportunities:
        assert opportunity.priority in {"contact_first", "worth_pursuing", "review", "skip"}
        assert opportunity.discovery_source == "fake_discovery"
        # Never a fabricated buyer name — see arie.discovery.models.BuyerSignal.
        assert opportunity.buyer is None or opportunity.buyer.name_known is False

    with discovery_pool.connection() as conn:
        candidates = repository.list_candidates(
            conn, run_id=run.run_id, organization_id=LEGACY_ORGANIZATION_ID
        )
    assert len(candidates) == funnel.unique_companies
    assert all(c.screening_class is not None for c in candidates)
    assert all(c.promoted_lead_id is not None for c in candidates)  # all promising in this test


def test_discovery_run_isolates_a_failing_provider_query(
    discovery_pool: ConnectionPool,
    job_queue: PostgresJobQueue,
    identity_resolver: IdentityResolver,
    discovery_cleanup: list[uuid.UUID],
) -> None:
    """One bad search query must not fail the whole run — the discovery
    provider layer isolates per-query errors, matching every other provider
    adapter's own "one failure doesn't fail the batch" rule."""

    class _FlakyProvider:
        name = "flaky"
        calls = 0

        def search(self, query: str, limit: int) -> list:  # type: ignore[type-arg]
            self.calls += 1
            if self.calls == 1:
                raise DiscoveryProviderError("simulated transport failure")
            return FakeDiscoveryProvider().search(query, limit)

    ledger = PostgresCostLedger(discovery_pool)

    def _two_query_plan(messages: object) -> str:
        return json.dumps(
            {
                "queries": [
                    {"query": "multi-location gyms Australia", "rationale": "a"},
                    {"query": "supplement distributors Australia", "rationale": "b"},
                ]
            }
        )

    llm = LLMService(
        discovery_pool, ledger=ledger, provider=FakeLLMProvider(handler=_two_query_plan)
    )

    with discovery_pool.connection() as conn:
        profile = get_active_profile(conn, organization_id=LEGACY_ORGANIZATION_ID)

    run, _opportunities = run_discovery(
        discovery_pool,
        resolver=identity_resolver,
        queue=job_queue,
        ledger=ledger,
        llm=llm,
        organization_id=LEGACY_ORGANIZATION_ID,
        profile=profile,
        requested_opportunity_count=3,
        market="Australia",
        max_candidates=10,
        created_by_user_id=None,
        now=datetime.now(UTC),
        discovery_provider=_FlakyProvider(),
    )
    discovery_cleanup.append(run.run_id)

    assert run.status is DiscoveryRunStatus.COMPLETE
    # The first query failed outright; the second still produced candidates.
    assert run.funnel.raw_candidates > 0


def test_discovery_run_via_http_api(
    app_state: AppState,
    discovery_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full `POST /discovery/runs` -> `GET .../{id}` ->
    `GET .../{id}/opportunities` -> `GET /discovery/runs` surface, over real
    HTTP against the real API app — not just the orchestrator function
    directly. The real `FirecrawlDiscoveryProvider` is monkeypatched out so
    this test never depends on network access or Firecrawl's availability."""
    monkeypatch.setattr(
        orchestrator_module, "build_discovery_provider", lambda: FakeDiscoveryProvider()
    )

    app = create_app(state=app_state)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        organization_id=LEGACY_ORGANIZATION_ID,
        auth_method="jwt",
        user_id=uuid.uuid4(),
        role="owner",
    )
    app.dependency_overrides[get_llm_service] = lambda: LLMService(
        app_state.pool, provider=FakeLLMProvider(handler=_classify_all_promising_handler)
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/discovery/runs",
            json={"requested_opportunity_count": 3, "market": "Australia", "max_candidates": 10},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        run_id = uuid.UUID(body["run"]["run_id"])
        discovery_cleanup.append(run_id)

        assert body["run"]["status"] == "complete"
        assert body["run"]["funnel"]["promoted_to_leads"] > 0
        assert len(body["opportunities"]) > 0

        get_response = client.get(f"/discovery/runs/{run_id}")
        assert get_response.status_code == 200
        assert get_response.json()["status"] == "complete"

        opportunities_response = client.get(f"/discovery/runs/{run_id}/opportunities")
        assert opportunities_response.status_code == 200
        assert len(opportunities_response.json()["opportunities"]) == len(body["opportunities"])

        list_response = client.get("/discovery/runs")
        assert list_response.status_code == 200
        assert any(row["run_id"] == str(run_id) for row in list_response.json())
