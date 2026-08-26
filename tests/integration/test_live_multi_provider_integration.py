"""Two real providers, one live lead — the whole acquisition path (Live V1).

This is the suite for everything that only exists once live mode has more than
one provider: whether the second one is called at all, what happens when it is
refused, what its evidence is scoped to, and whether the receipt tells the
difference. Its siblings stay narrow on purpose —
``test_live_provider_integration.py`` runs the company adapter in isolation and
``test_live_v1_foundation_integration.py`` owns the autonomy guard and the
spend caps as invariants.

**The question this file exists to answer.** A one-provider pipeline can only
ever report "called it" — "did ARIE decide it *needed* to spend?" is not a
question it can be asked. With Abstract and Apollo wired, it can: a lead that
firmographics already answer confidently must skip the person lookup, and a
lead they leave open must not. Both are asserted here against a real database,
a real ledger, and a real receipt, with both vendors' HTTP layers mocked —
nothing in this file makes a real API call or spends a real credit.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup, source_for

from arie.config import ApolloPersonConfig, LiveBudgetConfig, LiveProviderConfig
from arie.core.types import LeadStatus
from arie.evalgen.schema import EvalLead
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers, build_runtime
from arie.jobs.queue import ClaimedJob
from arie.jobs.worker import JobContext, JobHandler
from arie.live.budget import DAILY_BUDGET_EXHAUSTED, PER_LEAD_BUDGET_EXHAUSTED
from arie.live.safety import PERMITTED_LIVE_STATUSES
from arie.providers.base import EnrichmentProvider
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_apollo import APOLLO_PROVIDER_NAME as APOLLO
from arie.providers.live_apollo import ApolloPersonEnrichmentProvider

pytestmark = pytest.mark.integration

_TEST_WORKER_ID = "live-multi-provider-it"

_ABSTRACT_COST = 0.002
_APOLLO_COST = 0.02


# --------------------------------------------------------------- vendor mocks --


def _abstract_open(request: httpx.Request) -> httpx.Response:
    """A company that leaves the decision genuinely open.

    240 employees in prime-ICP software: a strong lead on firmographics, and
    precisely therefore *not* a confident one — the score lands near the
    qualify boundary with seniority and function still unknown, which is the
    situation person evidence exists to resolve. This is the CASE B fixture.
    """
    return httpx.Response(200, json={"employee_count": 240, "industry": "Computer Software"})


def _abstract_settles_it(request: httpx.Request) -> httpx.Response:
    """A company the firmographics already answer.

    A five-person construction firm is far outside the reference ICP, scores 2
    of 100, and sits nowhere near a decision boundary. The calibrated model is
    confident about *that* recommendation on company evidence alone, so buying
    a job title cannot change it — no seniority in the ruleset is worth enough
    to move this lead across the reject threshold. This is the CASE A fixture,
    and it is a real business case rather than a contrived one: most inbound is
    exactly this.
    """
    return httpx.Response(200, json={"employee_count": 5, "industry": "Construction"})


def _apollo_vp_sales(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "person": {
                "name": "Dana Okafor",
                "title": "VP of Sales",
                "seniority": "vp",
                "departments": ["sales"],
                "organization": {"name": "Northwind", "primary_domain": "northwind.test"},
            }
        },
    )


def _failing(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "nope"})

    return handler


def _counting(
    inner: Callable[[httpx.Request], httpx.Response], counter: list[int]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        counter[0] += 1
        return inner(request)

    return handler


def _abstract_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AbstractCompanyEnrichmentProvider:
    return AbstractCompanyEnrichmentProvider(
        config=LiveProviderConfig(api_key="test-key", cost_usd_per_call=_ABSTRACT_COST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _apollo_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> ApolloPersonEnrichmentProvider:
    return ApolloPersonEnrichmentProvider(
        config=ApolloPersonConfig(api_key="test-key", cost_usd_per_success=_APOLLO_COST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ------------------------------------------------------------------ scaffolding --


@pytest.fixture(scope="module")
def runtime(leads: list[EvalLead]) -> SimulatedEnrichmentRuntime:
    return build_runtime(leads=leads)


@pytest.fixture(scope="module")
def live_pool(migrated_database: str) -> Iterator[ConnectionPool]:
    """One pool for the module — same reasoning as
    ``test_live_v1_foundation_integration.py``: per-test pools churn backend
    sessions and the symptom of exhausting them is a job stuck ``processing``,
    which reads as a logic failure that did not happen."""
    pool = ConnectionPool(migrated_database, min_size=1, max_size=4, open=True)
    try:
        yield pool
    finally:
        pool.close()


def _handlers_for(
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
    providers: list[EnrichmentProvider],
    budget: LiveBudgetConfig | None = None,
) -> dict[str, JobHandler]:
    handlers = build_handlers(
        live_pool, runtime=runtime, provider_mode="live", live_providers=providers
    )
    if budget is not None:
        _patch_budget(handlers, live_pool, budget)
    return handlers


def _patch_budget(
    handlers: dict[str, JobHandler], live_pool: ConnectionPool, budget: LiveBudgetConfig
) -> None:
    """Rebuild the handler with a tighter cap by rebuilding its guard.

    ``LiveSpendGuard`` reads its config at construction, and the handler closes
    over one instance — so a budget test cannot patch the module singleton
    after the fact. Reaching into the closure is the alternative to threading a
    budget parameter through ``build_handlers`` purely for tests, which would
    put a test-shaped argument in production's signature.
    """
    from arie.live.budget import LiveSpendGuard

    handler = handlers["compute_score"]
    closure = handler.__closure__ or ()
    for cell in closure:
        if isinstance(cell.cell_contents, LiveSpendGuard):
            cell.cell_contents = LiveSpendGuard(live_pool, budget)
            return
    raise AssertionError("compute_score no longer closes over a LiveSpendGuard")


def _ingest(
    api_client: TestClient,
    cleanup: IngestCleanup,
    *,
    prefix: str,
    domain: str | None = None,
    email: str | None = None,
    mode: str = "normal",
) -> dict[str, Any]:
    resolved_domain = domain or f"{prefix}-{uuid.uuid4().hex[:10]}.test"
    resolved_email = email or f"nobody-{uuid.uuid4().hex[:8]}@{resolved_domain}"
    cleanup.domains.append(resolved_domain)
    cleanup.emails.append(resolved_email)
    response = api_client.post(
        "/leads",
        json={
            "source": source_for("live-multi"),
            "email": resolved_email,
            "external_ref": f"live-multi-{uuid.uuid4().hex[:12]}",
            "company_domain": resolved_domain,
            "mode": mode,
        },
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    cleanup.lead_ids.append(uuid.UUID(body["lead_id"]))
    return body


def _take_ownership(db_conn: psycopg.Connection, job_id: str) -> None:
    """Claim this test's job before a deployed worker can — see
    ``test_live_v1_foundation_integration._take_ownership`` for the full
    argument. Skips loudly naming the thief rather than reporting a failure
    that is really a race."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'processing', locked_by = %(worker)s, locked_at = now() "
            "WHERE job_id = %(job_id)s AND status = 'pending'",
            {"worker": _TEST_WORKER_ID, "job_id": job_id},
        )
        taken = cur.rowcount
        cur.execute("SELECT locked_by FROM jobs WHERE job_id = %(job_id)s", {"job_id": job_id})
        row = cur.fetchone()
    db_conn.commit()
    if not taken:
        owner = row[0] if row else "unknown"
        pytest.skip(f"another worker ({owner}) claimed job {job_id} before this test could")


def _process_lead(
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    db_conn: psycopg.Connection,
    body: dict[str, Any],
) -> None:
    job_id = uuid.UUID(body["job_id"])
    lead_id = uuid.UUID(body["lead_id"])

    with live_pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, version FROM leads WHERE lead_id = %s", (lead_id,))
            row = cur.fetchone()
        assert row is not None
        handlers["compute_score"](
            JobContext(
                conn=conn,
                job=ClaimedJob(
                    job_id=job_id,
                    lead_id=lead_id,
                    job_type="compute_score",
                    attempt_count=0,
                    idempotency_key=None,
                ),
                lead_status=LeadStatus(row[0]),
                lead_version=row[1],
            )
        )
        conn.commit()

    with db_conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'done' WHERE job_id = %s", (job_id,))
    db_conn.commit()


def _register_cleanup(
    db_conn: psycopg.Connection,
    cleanup: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    body: dict[str, Any],
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key FROM provider_calls WHERE lead_id = %s", (body["lead_id"],)
        )
        cleanup.provider_call_keys.extend(row[0] for row in cur.fetchall())
    cleanup_evidence.append(uuid.UUID(body["company_id"]))
    cleanup_evidence.append(uuid.UUID(body["person_id"]))


def _run(
    api_client: TestClient,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    db_conn: psycopg.Connection,
    live_pool: ConnectionPool,
    handlers: dict[str, JobHandler],
    *,
    prefix: str,
    domain: str | None = None,
    email: str | None = None,
    mode: str = "normal",
) -> dict[str, Any]:
    body = _ingest(api_client, cleanup_ingest, prefix=prefix, domain=domain, email=email, mode=mode)
    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, handlers, db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)
    return body


def _receipt(api_client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    return payload


def _called(receipt: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {call["provider"]: call for call in receipt["providers"]["called"]}


def _evidence_sources(db_conn: psycopg.Connection, entity_id: str) -> dict[str, str]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT field_name, source FROM evidence WHERE entity_id = %s", (uuid.UUID(entity_id),)
        )
        return {row[0]: row[1] for row in cur.fetchall()}


# ============================================================== CASE A / CASE B --


def test_case_b_an_open_company_decision_triggers_the_person_lookup(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Abstract answers, the decision stays open, Apollo is called.

    The proof that a second provider is reachable at all — and, read together
    with CASE A below, that reaching it is a *decision* rather than a fixed
    sequence. Both providers' evidence must land, each under its own source, on
    its own entity.
    """
    abstract_calls, apollo_calls = [0], [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_counting(_abstract_open, abstract_calls)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="caseb"
    )

    assert abstract_calls[0] == 1
    assert apollo_calls[0] == 1

    company = _evidence_sources(db_conn, body["company_id"])
    person = _evidence_sources(db_conn, body["person_id"])
    assert company == {"employee_count": ABSTRACT, "industry": ABSTRACT}
    assert person == {"title_seniority": APOLLO, "title_function": APOLLO}

    receipt = _receipt(api_client, body)
    calls = _called(receipt)
    assert set(calls) == {ABSTRACT, APOLLO}
    assert receipt["providers"]["not_called"] == []
    assert receipt["stopping"]["reason_code"] == "all_providers_called"
    # Both fields the person provider exists to supply are now known, and the
    # receipt says which vendor each came from — not "the live provider".
    known = {item["field"]: item["source"] for item in receipt["evidence"]["items"]}
    assert known["title_seniority"] == APOLLO
    assert known["industry"] == ABSTRACT


def test_case_a_a_settled_company_decision_skips_the_person_lookup_entirely(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The point of the whole exercise: ARIE declines to spend.

    A five-person construction firm is a confident reject on firmographics
    alone. Apollo is never contacted, no credit is consumed, no person evidence
    is written — and the receipt reports it as *not called* with a stop reason
    that says why, rather than leaving a reader to guess whether the provider
    was broken, unconfigured, or simply unnecessary.
    """
    apollo_calls = [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_settles_it),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="casea"
    )

    assert apollo_calls[0] == 0
    assert _evidence_sources(db_conn, body["person_id"]) == {}

    receipt = _receipt(api_client, body)
    assert set(_called(receipt)) == {ABSTRACT}
    assert receipt["providers"]["not_called"] == [APOLLO]
    assert receipt["stopping"]["reason_code"] == "confidence_reached"
    assert float(receipt["cost"]["provider_cost_usd"]) == pytest.approx(_ABSTRACT_COST)


def test_the_two_cases_differ_only_in_what_the_company_provider_returned(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Same code path, same providers, same budget — opposite outcomes.

    Asserted as one test rather than inferred from two, because the claim being
    made is comparative: the skip is caused by the *evidence*, not by
    configuration differing between the runs. If someone later disables Apollo
    by config and both cases keep passing individually, this one fails.
    """
    open_calls, settled_calls = [0], [0]

    open_body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _handlers_for(
            live_pool,
            runtime,
            [
                _abstract_provider(_abstract_open),
                _apollo_provider(_counting(_apollo_vp_sales, open_calls)),
            ],
        ),
        prefix="cmp-open",
    )
    settled_body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _handlers_for(
            live_pool,
            runtime,
            [
                _abstract_provider(_abstract_settles_it),
                _apollo_provider(_counting(_apollo_vp_sales, settled_calls)),
            ],
        ),
        prefix="cmp-settled",
    )

    assert (open_calls[0], settled_calls[0]) == (1, 0)
    assert _receipt(api_client, open_body)["stopping"]["reason_code"] == "all_providers_called"
    assert _receipt(api_client, settled_body)["stopping"]["reason_code"] == "confidence_reached"


# ================================================================ budget guards --


def test_apollo_is_refused_when_the_per_lead_budget_cannot_cover_it(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """A cap set between the two prices: Abstract fits, Apollo does not.

    The interesting shape, and the one a single-provider pipeline could not
    produce — partial acquisition. The cheap call happens, the expensive one is
    refused *before* it is made, and the lead is decided on what was actually
    bought. No partial spend beyond the cap: total cost is exactly Abstract's.
    """
    apollo_calls = [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
        budget=LiveBudgetConfig(daily_usd=5.00, per_lead_usd=_ABSTRACT_COST + 0.001),
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="perlead"
    )

    assert apollo_calls[0] == 0
    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == PER_LEAD_BUDGET_EXHAUSTED
    assert set(_called(receipt)) == {ABSTRACT}
    assert float(receipt["cost"]["provider_cost_usd"]) == pytest.approx(_ABSTRACT_COST)
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES


def test_an_exhausted_daily_budget_stops_acquisition_before_the_first_call(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """A zero daily cap refuses everything, and the lead still gets a receipt.

    The failure mode being excluded is a lead that silently disappears when the
    account runs dry. It is decided on no purchased evidence at all, which is a
    bad decision honestly labelled — and it still reaches a human.
    """
    abstract_calls, apollo_calls = [0], [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_counting(_abstract_open, abstract_calls)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
        budget=LiveBudgetConfig(daily_usd=0.0, per_lead_usd=0.0),
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="daily"
    )

    assert (abstract_calls[0], apollo_calls[0]) == (0, 0)
    receipt = _receipt(api_client, body)
    # Per-lead is checked first and both caps are zero, so the *tighter*
    # constraint is the one named — the guard's documented precedence.
    assert receipt["stopping"]["reason_code"] in {
        PER_LEAD_BUDGET_EXHAUSTED,
        DAILY_BUDGET_EXHAUSTED,
    }
    assert receipt["providers"]["called"] == []
    assert sorted(receipt["providers"]["not_called"]) == sorted([ABSTRACT, APOLLO])
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES


# ================================================================= cache scoping --


def test_a_colleague_reuses_company_evidence_and_is_still_looked_up_individually(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The cache-scoping guarantee, stated as the behaviour a user would notice.

    Two people at one employer. The second must reuse the firmographics — that
    is the saving company-level caching exists for — and must NOT inherit the
    first person's job title, which would be a fabricated fact about a real
    individual. Person evidence keys on ``person_id``, so this holds by
    construction; it is asserted because "by construction" is exactly the kind
    of claim a later refactor of the evidence lookup could quietly break.
    """
    domain = f"colleagues-{uuid.uuid4().hex[:10]}.test"
    abstract_calls, apollo_calls = [0], [0]

    def _apollo_director(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"person": {"title": "Director of Marketing", "name": "Second Person"}}
        )

    first = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _handlers_for(
            live_pool,
            runtime,
            [
                _abstract_provider(_counting(_abstract_open, abstract_calls)),
                _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
            ],
        ),
        prefix="colleague-a",
        domain=domain,
        email=f"first-{uuid.uuid4().hex[:8]}@{domain}",
    )
    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _handlers_for(
            live_pool,
            runtime,
            [
                _abstract_provider(_counting(_abstract_open, abstract_calls)),
                _apollo_provider(_counting(_apollo_director, apollo_calls)),
            ],
        ),
        prefix="colleague-b",
        domain=domain,
        email=f"second-{uuid.uuid4().hex[:8]}@{domain}",
    )

    assert first["company_id"] == second["company_id"]
    assert first["person_id"] != second["person_id"]

    # Company evidence was bought once and reused; person evidence was bought
    # for each individual.
    assert abstract_calls[0] == 1
    assert apollo_calls[0] == 2

    first_person = _evidence_sources(db_conn, first["person_id"])
    second_person = _evidence_sources(db_conn, second["person_id"])
    assert set(first_person) == {"title_seniority", "title_function"}
    assert set(second_person) == {"title_seniority", "title_function"}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT entity_id, value FROM evidence WHERE field_name = 'title_seniority' "
            "AND entity_id = ANY(%s)",
            ([uuid.UUID(first["person_id"]), uuid.UUID(second["person_id"])],),
        )
        seniorities = {str(row[0]): row[1] for row in cur.fetchall()}
    assert seniorities[first["person_id"]] == "vp"
    assert seniorities[second["person_id"]] == "director"

    second_receipt = _receipt(api_client, second)
    abstract_call = _called(second_receipt)[ABSTRACT]
    assert abstract_call["cache_hit"] is True
    assert float(abstract_call["cost_usd"]) == 0.0


# ==================================================================== failures --


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_a_person_provider_failure_never_loses_the_lead(
    status_code: int,
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Apollo breaks after Abstract succeeded: the company evidence survives,
    the failure is ledgered at zero cost, and the lead reaches a human.

    ``provider_failed`` rather than ``all_providers_called`` — the second would
    claim the seniority evidence does not exist, when in fact ARIE failed to
    fetch it.
    """
    handlers = _handlers_for(
        live_pool,
        runtime,
        [_abstract_provider(_abstract_open), _apollo_provider(_failing(status_code))],
    )
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        prefix=f"fail{status_code}",
    )

    assert _evidence_sources(db_conn, body["company_id"]) == {
        "employee_count": ABSTRACT,
        "industry": ABSTRACT,
    }
    assert _evidence_sources(db_conn, body["person_id"]) == {}

    receipt = _receipt(api_client, body)
    apollo_call = _called(receipt)[APOLLO]
    assert apollo_call["status"] == "error"
    assert float(apollo_call["cost_usd"]) == 0.0
    assert receipt["stopping"]["reason_code"] == "provider_failed"
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES


def test_a_company_provider_failure_does_not_stop_the_person_provider(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """One vendor being down is not a reason to skip a different vendor that is
    up. Abstract 500s; Apollo still runs and its evidence still lands. The stop
    reason stays ``provider_failed`` because the lead was genuinely decided on
    incomplete information."""
    apollo_calls = [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_failing(500)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="absdown"
    )

    assert apollo_calls[0] == 1
    assert _evidence_sources(db_conn, body["person_id"]) == {
        "title_seniority": APOLLO,
        "title_function": APOLLO,
    }
    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == "provider_failed"
    assert _called(receipt)[ABSTRACT]["status"] == "error"
    assert _called(receipt)[APOLLO]["status"] == "success"


def test_a_person_apollo_cannot_find_is_a_free_miss_not_a_failure(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Apollo has no record of this person. It consumed no credit, so the
    ledger must show zero — reporting a modelled cost here would invent spend
    that did not happen. And the lead is not "failed": every provider answered."""

    def _not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"person": None})

    handlers = _handlers_for(
        live_pool, runtime, [_abstract_provider(_abstract_open), _apollo_provider(_not_found)]
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="nomatch"
    )

    receipt = _receipt(api_client, body)
    apollo_call = _called(receipt)[APOLLO]
    assert apollo_call["status"] == "miss"
    assert float(apollo_call["cost_usd"]) == 0.0
    assert float(receipt["cost"]["provider_cost_usd"]) == pytest.approx(_ABSTRACT_COST)
    assert receipt["stopping"]["reason_code"] == "all_providers_called"
    assert "title_seniority" in receipt["evidence"]["unknown_fields"]


def test_a_free_mail_lead_skips_the_company_provider_and_still_gets_person_evidence(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Previously a dead end. A gmail.com lead resolves no company domain, so
    the domain-keyed provider is genuinely uncallable — but the person provider
    keys on the email, which every ingested lead has. Before Apollo existed
    this lead stopped at ``no_domain_available`` with nothing bought; now the
    reachable half of the pipeline runs."""
    email = f"solo-{uuid.uuid4().hex[:10]}@gmail.com"
    cleanup_ingest.emails.append(email)
    abstract_calls, apollo_calls = [0], [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_counting(_abstract_open, abstract_calls)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )

    response = api_client.post(
        "/leads",
        json={
            "source": source_for("live-multi"),
            "email": email,
            "external_ref": f"live-multi-{uuid.uuid4().hex[:12]}",
            "company_name": f"Solo Co {uuid.uuid4().hex[:8]}",
        },
    )
    assert response.status_code == 201
    body = response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(body["lead_id"]))
    from arie.identity.normalize import normalize_company_name

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT normalized_name FROM companies WHERE company_id = %s",
            (uuid.UUID(body["company_id"]),),
        )
        row = cur.fetchone()
    if row is not None:
        cleanup_ingest.company_names.append(normalize_company_name(row[0]))

    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, handlers, db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    assert abstract_calls[0] == 0
    assert apollo_calls[0] == 1
    assert _evidence_sources(db_conn, body["person_id"]) == {
        "title_seniority": APOLLO,
        "title_function": APOLLO,
    }

    receipt = _receipt(api_client, body)
    assert set(_called(receipt)) == {APOLLO}
    assert receipt["providers"]["not_called"] == [ABSTRACT]
    # NOT `no_domain_available`: something was reachable and was reached. That
    # code is now reserved for a lead no provider could serve at all.
    assert receipt["stopping"]["reason_code"] == "all_providers_called"


# ===================================================================== autonomy --


def test_two_providers_do_not_earn_a_live_lead_the_right_to_act(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """More evidence is not evidence that the threshold transfers.

    The autonomy guard's argument is about the *calibration* of the confidence
    model, not about how much was collected — widening coverage from one
    provider to two does nothing to validate a threshold fitted on synthetic
    data. A strong, fully-enriched lead is where a guard regression would show
    up first, so it is asserted on exactly that lead.
    """
    handlers = _handlers_for(
        live_pool,
        runtime,
        [_abstract_provider(_abstract_open), _apollo_provider(_apollo_vp_sales)],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="guard"
    )

    receipt = _receipt(api_client, body)
    assert LeadStatus(receipt["lead_status"]) is LeadStatus.AWAITING_HUMAN
    assert receipt["decision"]["autonomous"] is False
    assert receipt["human_review"]["required"] is True
