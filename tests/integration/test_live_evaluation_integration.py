"""Three providers, two strategies — the evaluation mode and the full waterfall.

Sibling to ``test_live_multi_provider_integration.py`` (which owns the
two-provider optimized behaviour) and reuses its scaffolding. This file owns
what exists only since Hunter and the strategy split:

* the full optimized chain Abstract → Hunter → Apollo, including Hunter
  satisfying the person fields so Apollo is skipped as *redundant* (no
  fabricated ledger row) and Hunter missing so Apollo is reached;
* ``evaluation_parallel`` — both person providers called concurrently for one
  lead, each ledgered separately, agreement classified, conflicts contested,
  the receipt self-identifying via ``versions.policy``;
* quota exhaustion and the ledger-backed cooldown — a provider that reported
  its allowance spent is not re-dialled by the next lead, and the pipeline
  continues on the remaining vendors;
* the evaluation budget as a separate explicit cap, still enforced by the same
  guard.

Every vendor HTTP layer is mocked; nothing here spends a credit. And under
both strategies every lead still terminates at a human — asserted again here
because more providers and more strategies are precisely the conditions under
which an autonomy regression would first appear.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from tests.integration.conftest import IngestCleanup
from tests.integration.test_live_multi_provider_integration import (
    _abstract_open,
    _abstract_provider,
    _abstract_settles_it,
    _apollo_provider,
    _apollo_vp_sales,
    _counting,
    _failing,
    _handlers_for,
    _ingest,
    _process_lead,
    _register_cleanup,
    _take_ownership,
)
from tests.integration.test_live_multi_provider_integration import (
    live_pool as live_pool,
)
from tests.integration.test_live_multi_provider_integration import (
    runtime as runtime,
)

from arie.config import HunterConfig, LiveBudgetConfig
from arie.core.types import LeadStatus
from arie.jobs.handlers import SimulatedEnrichmentRuntime, build_handlers
from arie.jobs.worker import JobHandler
from arie.live.budget import PER_LEAD_BUDGET_EXHAUSTED
from arie.live.cooldown import PROVIDER_UNAVAILABLE
from arie.live.safety import PERMITTED_LIVE_STATUSES
from arie.live.strategy import EVALUATION_POLICY_NAME, OPTIMIZED_POLICY_NAME
from arie.providers.base import EnrichmentProvider
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME as HUNTER
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT
from arie.providers.live_apollo import APOLLO_PROVIDER_NAME as APOLLO
from arie.providers.live_hunter import HunterEnrichmentProvider

pytestmark = pytest.mark.integration

_HUNTER_COST = 0.005


def _hunter_provider(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HunterEnrichmentProvider:
    return HunterEnrichmentProvider(
        config=HunterConfig(api_key="test-key", cost_usd_per_success=_HUNTER_COST),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _hunter_person(
    title: str, *, seniority: str | None = None, role: str | None = None
) -> dict[str, Any]:
    employment: dict[str, Any] = {"name": "Northwind", "title": title}
    if seniority is not None:
        employment["seniority"] = seniority
    if role is not None:
        employment["role"] = role
    return {"data": {"person": {"name": {"fullName": "Dana Okafor"}, "employment": employment}}}


def _hunter_vp_sales(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_hunter_person("VP of Sales", role="sales"))


def _hunter_director_marketing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_hunter_person("Director of Marketing", role="marketing"))


def _hunter_not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"errors": [{"id": "not_found", "code": 404}]})


def _apollo_not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"person": None})


def _quota(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "quota"})

    return handler


def _eval_handlers(
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
    providers: list[EnrichmentProvider],
) -> dict[str, JobHandler]:
    return build_handlers(
        live_pool,
        runtime=runtime,
        provider_mode="live",
        live_providers=providers,
        live_strategy="evaluation_parallel",
    )


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
) -> dict[str, Any]:
    body = _ingest(api_client, cleanup_ingest, prefix=prefix, domain=domain, email=email)
    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, handlers, db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)
    return body


def _receipt(api_client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = api_client.get(f"/leads/{body['lead_id']}/receipt").json()
    return payload


def _calls(db_conn: psycopg.Connection, lead_id: str) -> dict[str, dict[str, Any]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT provider, status, cost_usd, cache_hit, error_kind, credits_used, cost_basis "
            "FROM provider_calls WHERE lead_id = %s",
            (uuid.UUID(lead_id),),
        )
        return {
            row[0]: {
                "status": row[1],
                "cost_usd": float(row[2]),
                "cache_hit": row[3],
                "error_kind": row[4],
                "credits_used": None if row[5] is None else float(row[5]),
                "cost_basis": row[6],
            }
            for row in cur.fetchall()
        }


def _snapshot(db_conn: psycopg.Connection, lead_id: str) -> dict[str, Any]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT evidence_snapshot FROM decision_receipts WHERE lead_id = %s",
            (uuid.UUID(lead_id),),
        )
        row = cur.fetchone()
    assert row is not None
    snapshot: dict[str, Any] = row[0]
    return snapshot


# ================================================== optimized, three providers --


def test_optimized_hunter_success_makes_apollo_redundant_with_no_ledger_row(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Abstract → Hunter answers both person fields → Apollo is never called.

    And crucially never *ledgered*: with two person providers selling the same
    fields, a fabricated "cache hit" row for Apollo would attribute Hunter's
    evidence to Apollo. The receipt reports Apollo under not_called, and the
    only person evidence carries Hunter's name.
    """
    apollo_calls = [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_vp_sales),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="redun"
    )

    assert apollo_calls[0] == 0
    calls = _calls(db_conn, body["lead_id"])
    assert set(calls) == {ABSTRACT, HUNTER}
    assert calls[HUNTER]["status"] == "success"
    assert calls[HUNTER]["credits_used"] == pytest.approx(0.2)
    assert calls[HUNTER]["cost_basis"] == "modelled_credit_equivalent"

    receipt = _receipt(api_client, body)
    assert receipt["versions"]["policy"] == OPTIMIZED_POLICY_NAME
    assert APOLLO in receipt["providers"]["not_called"]
    known = {item["field"]: item["source"] for item in receipt["evidence"]["items"]}
    assert known["title_seniority"] == HUNTER
    assert known["title_function"] == HUNTER


def test_optimized_hunter_miss_falls_through_to_apollo(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The waterfall's point: the cheap person provider gets first refusal,
    and its miss (a free 404) hands the question to the expensive one."""
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_not_found),
            _apollo_provider(_apollo_vp_sales),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="chain"
    )

    calls = _calls(db_conn, body["lead_id"])
    assert set(calls) == {ABSTRACT, HUNTER, APOLLO}
    assert calls[HUNTER]["status"] == "miss"
    assert calls[HUNTER]["cost_usd"] == 0.0  # Hunter's no-match consumes nothing
    assert calls[APOLLO]["status"] == "success"
    assert calls[APOLLO]["credits_used"] == pytest.approx(1.0)

    receipt = _receipt(api_client, body)
    known = {item["field"]: item["source"] for item in receipt["evidence"]["items"]}
    assert known["title_seniority"] == APOLLO
    assert receipt["stopping"]["reason_code"] == "all_providers_called"


def test_optimized_case_a_still_skips_both_person_providers(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """A confident reject on firmographics now saves TWO person lookups."""
    hunter_calls, apollo_calls = [0], [0]
    handlers = _handlers_for(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_settles_it),
            _hunter_provider(_counting(_hunter_vp_sales, hunter_calls)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="skip2"
    )

    assert (hunter_calls[0], apollo_calls[0]) == (0, 0)
    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == "confidence_reached"
    assert set(receipt["providers"]["not_called"]) == {HUNTER, APOLLO}


# ==================================================== evaluation_parallel mode --


def test_evaluation_calls_both_person_providers_and_classifies_agreement(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Both vendors answer the same question about the same person — the
    overlap optimized mode exists to avoid, deliberately bought here. Each call
    is ledgered separately with its own cost and credits; both evidence rows
    persist under their own sources; the receipt self-identifies as an
    evaluation run; and matching answers classify as AGREE."""
    hunter_calls, apollo_calls = [0], [0]
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_counting(_hunter_vp_sales, hunter_calls)),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="evalok"
    )

    assert (hunter_calls[0], apollo_calls[0]) == (1, 1)
    calls = _calls(db_conn, body["lead_id"])
    assert calls[HUNTER]["status"] == "success"
    assert calls[APOLLO]["status"] == "success"
    assert calls[HUNTER]["cost_usd"] == pytest.approx(_HUNTER_COST)
    assert calls[APOLLO]["cost_usd"] == pytest.approx(0.02)

    receipt = _receipt(api_client, body)
    assert receipt["versions"]["policy"] == EVALUATION_POLICY_NAME
    assert receipt["stopping"]["reason_code"] == "evaluation_complete"

    snapshot = _snapshot(db_conn, body["lead_id"])
    evaluation = snapshot["evaluation"]
    assert evaluation["strategy"] == "evaluation_parallel"
    assert evaluation["agreement"]["title_seniority"] == "agree"
    assert evaluation["agreement"]["title_function"] == "agree"
    assert evaluation["agreement"]["overall"] == "agree"
    assert evaluation["person_providers"][HUNTER]["served_from"] == "live_call"
    assert evaluation["person_providers"][HUNTER]["raw_title"] == "VP of Sales"

    # Both provenance rows persist — neither vendor's answer overwrote the
    # other's.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source FROM evidence WHERE entity_id = %s AND field_name = 'title_seniority'",
            (uuid.UUID(body["person_id"]),),
        )
        sources = {row[0] for row in cur.fetchall()}
    assert sources == {HUNTER, APOLLO}


def test_evaluation_conflict_is_contested_not_silently_resolved(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Hunter says director/marketing, Apollo says vp/sales. Neither vendor
    wins by default: the conflict is classified, both provenance rows survive,
    the merge layer marks the fields contested on the receipt, and the lead is
    in front of a human — which live mode guarantees anyway, but a conflict is
    the case where that guarantee is carrying real weight."""
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_director_marketing),
            _apollo_provider(_apollo_vp_sales),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="evalcf"
    )

    snapshot = _snapshot(db_conn, body["lead_id"])
    assert snapshot["evaluation"]["agreement"]["title_seniority"] == "conflict"
    assert snapshot["evaluation"]["agreement"]["title_function"] == "conflict"
    assert snapshot["evaluation"]["agreement"]["overall"] == "conflict"

    receipt = _receipt(api_client, body)
    contested = {item["field"] for item in receipt["evidence"]["items"] if item.get("contested")}
    # Two deliberate notions of disagreement, and this fixture separates them:
    # the evaluation classifier reports a VENDOR conflict wherever canonical
    # values differ (both fields above), while the merge layer's `contested`
    # is SCORE-relevant disagreement only — vp (18.0) vs director (14.0)
    # contests seniority, but sales and marketing both score 5.0, so that
    # conflict cannot move the score and the receipt honestly doesn't flag it.
    assert "title_seniority" in contested
    assert "title_function" not in contested
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES
    assert receipt["human_review"]["required"] is True


@pytest.mark.parametrize(
    ("hunter_handler", "apollo_handler", "surviving", "broken"),
    [
        (_failing(500), _apollo_vp_sales, APOLLO, HUNTER),
        (_hunter_vp_sales, _failing(500), HUNTER, APOLLO),
    ],
)
def test_evaluation_one_provider_failing_never_cancels_the_other(
    hunter_handler: Callable[[httpx.Request], httpx.Response],
    apollo_handler: Callable[[httpx.Request], httpx.Response],
    surviving: str,
    broken: str,
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Failure isolation, both directions: the broken vendor's error is
    ledgered at zero cost, the healthy vendor's evidence lands, and the stop
    reason says the lead was decided on incomplete information."""
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(hunter_handler),
            _apollo_provider(apollo_handler),
        ],
    )
    body = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        prefix=f"iso-{broken[:6]}",
    )

    calls = _calls(db_conn, body["lead_id"])
    assert calls[broken]["status"] == "error"
    assert calls[broken]["cost_usd"] == 0.0
    assert calls[broken]["error_kind"] == "server_error"
    assert calls[surviving]["status"] == "success"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT source FROM evidence WHERE entity_id = %s",
            (uuid.UUID(body["person_id"]),),
        )
        sources = {row[0] for row in cur.fetchall()}
    assert sources == {surviving}

    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == "provider_failed"
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES


def test_evaluation_both_missing_is_an_honest_unknown(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_not_found),
            _apollo_provider(_apollo_not_found),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="evalmm"
    )

    calls = _calls(db_conn, body["lead_id"])
    assert calls[HUNTER]["status"] == "miss"
    assert calls[APOLLO]["status"] == "miss"
    assert calls[HUNTER]["cost_usd"] == 0.0
    assert calls[APOLLO]["cost_usd"] == 0.0

    snapshot = _snapshot(db_conn, body["lead_id"])
    assert snapshot["evaluation"]["agreement"]["overall"] == "unknown"
    receipt = _receipt(api_client, body)
    assert "title_seniority" in receipt["evidence"]["unknown_fields"]
    assert receipt["stopping"]["reason_code"] == "evaluation_complete"


def test_evaluation_budget_is_enforced_by_the_same_guard(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """An evaluation cap sized for Abstract+Hunter but not Apollo: the cheap
    calls proceed, the expensive one is refused BEFORE being made, cumulatively
    — the parallel phase cannot admit two calls whose sum busts the cap."""
    from tests.integration.test_live_multi_provider_integration import _patch_budget

    apollo_calls = [0]
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_vp_sales),
            _apollo_provider(_counting(_apollo_vp_sales, apollo_calls)),
        ],
    )
    cap = 0.002 + _HUNTER_COST + 0.001  # covers Abstract + Hunter, not Apollo
    _patch_budget(
        handlers,
        live_pool,
        LiveBudgetConfig(daily_usd=5.0, per_lead_usd=cap, evaluation_per_lead_usd=cap),
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="evalbg"
    )

    assert apollo_calls[0] == 0
    calls = _calls(db_conn, body["lead_id"])
    assert set(calls) == {ABSTRACT, HUNTER}

    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == PER_LEAD_BUDGET_EXHAUSTED
    snapshot = _snapshot(db_conn, body["lead_id"])
    assert snapshot["evaluation"]["person_providers"][APOLLO]["served_from"] == "skipped_budget"


def test_evaluation_mode_is_still_never_autonomous(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """The most evidence any live lead can currently have — three providers,
    full agreement — and the guard still holds. A strategy is an acquisition
    behaviour; autonomy is upstream of strategy and reads none of it."""
    handlers = _eval_handlers(
        live_pool,
        runtime,
        [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_vp_sales),
            _apollo_provider(_apollo_vp_sales),
        ],
    )
    body = _run(
        api_client, cleanup_ingest, cleanup_evidence, db_conn, live_pool, handlers, prefix="evalau"
    )

    receipt = _receipt(api_client, body)
    assert LeadStatus(receipt["lead_status"]) is LeadStatus.AWAITING_HUMAN
    assert receipt["decision"]["autonomous"] is False
    assert receipt["human_review"]["required"] is True


# ================================================================ quota walls --


@pytest.mark.parametrize(
    ("wall_status", "walled"),
    [
        # Apollo's insufficient-credits convention is 402; Hunter's documented
        # quota status is 429. Each vendor's wall, each vendor's code.
        (402, APOLLO),
        (429, HUNTER),
    ],
)
def test_a_quota_wall_is_ledgered_once_then_cooled_down_not_hammered(
    wall_status: int,
    walled: str,
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Lead 1 (evaluation mode, so both person providers are attempted) hits
    the wall: the quota error is ledgered truthfully (``error_kind =
    quota_exhausted``, zero cost) and the other person provider still answers.
    Lead 2, a different person, fresh handler instances: the walled provider is
    NOT re-dialled — the cooldown guard reads the quota row back out of the
    durable ledger, the same way a sibling worker would — and the skip is
    recorded in the evaluation record, not invented as a call. No retry storm:
    across both leads the wall was hit exactly once."""
    walled_calls = [0]

    def providers() -> list[EnrichmentProvider]:
        walled_handler = _counting(_quota(wall_status), walled_calls)
        if walled == HUNTER:
            return [
                _abstract_provider(_abstract_open),
                _hunter_provider(walled_handler),
                _apollo_provider(_apollo_vp_sales),
            ]
        return [
            _abstract_provider(_abstract_open),
            _hunter_provider(_hunter_vp_sales),
            _apollo_provider(walled_handler),
        ]

    surviving = APOLLO if walled == HUNTER else HUNTER

    first = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _eval_handlers(live_pool, runtime, providers()),
        prefix=f"wall{wall_status}a",
    )

    first_calls = _calls(db_conn, first["lead_id"])
    assert first_calls[walled]["status"] == "error"
    assert first_calls[walled]["error_kind"] == "quota_exhausted"
    assert first_calls[walled]["cost_usd"] == 0.0
    assert first_calls[surviving]["status"] == "success"
    assert walled_calls[0] == 1

    second = _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        _eval_handlers(live_pool, runtime, providers()),
        prefix=f"wall{wall_status}b",
    )

    assert walled_calls[0] == 1  # the wall was never re-hit
    second_calls = _calls(db_conn, second["lead_id"])
    assert walled not in second_calls  # no call, and no fabricated row either
    assert second_calls[surviving]["status"] == "success"

    snapshot = _snapshot(db_conn, second["lead_id"])
    walled_record = snapshot["evaluation"]["person_providers"][walled]
    assert walled_record["served_from"] == "skipped_quota_cooldown"
    assert "cooling_down_until" in walled_record

    receipt = _receipt(api_client, second)
    assert receipt["stopping"]["reason_code"] == PROVIDER_UNAVAILABLE
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES


def test_a_cooled_down_provider_that_was_the_only_option_reports_unavailable(
    api_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_evidence: list[uuid.UUID],
    live_pool: ConnectionPool,
    runtime: SimulatedEnrichmentRuntime,
) -> None:
    """Free-mail lead (person provider only) whose sole person provider is in
    cooldown: acquisition ends with ``provider_unavailable`` — not
    ``all_providers_called`` (it wasn't consulted) and not ``provider_failed``
    (it didn't fail on this lead) — and the lead still reaches a human."""
    from arie.identity.normalize import normalize_company_name

    apollo_calls = [0]

    def providers() -> list[EnrichmentProvider]:
        return [
            _abstract_provider(_abstract_open),
            _apollo_provider(_counting(_quota(402), apollo_calls)),
        ]

    # Lead 1: burn the quota (normal domain lead so Apollo gets called).
    handlers = _handlers_for(live_pool, runtime, providers())
    _run(
        api_client,
        cleanup_ingest,
        cleanup_evidence,
        db_conn,
        live_pool,
        handlers,
        prefix="wallonly-a",
    )
    assert apollo_calls[0] == 1

    # Lead 2: free-mail (no company domain), so Apollo is the only reachable
    # provider — and it is cooling down.
    email = f"solo-{uuid.uuid4().hex[:10]}@gmail.com"
    company_name = f"Solo Co {uuid.uuid4().hex[:8]}"
    cleanup_ingest.emails.append(email)
    cleanup_ingest.company_names.append(normalize_company_name(company_name))
    response = api_client.post(
        "/leads",
        json={
            "source": "it-live-multi",
            "email": email,
            "external_ref": f"live-multi-{uuid.uuid4().hex[:12]}",
            "company_name": company_name,
        },
    )
    assert response.status_code == 201
    body = response.json()
    cleanup_ingest.lead_ids.append(uuid.UUID(body["lead_id"]))
    _take_ownership(db_conn, body["job_id"])
    _process_lead(live_pool, _handlers_for(live_pool, runtime, providers()), db_conn, body)
    _register_cleanup(db_conn, cleanup_ingest, cleanup_evidence, body)

    assert apollo_calls[0] == 1  # still exactly one — the wall was not re-hit
    receipt = _receipt(api_client, body)
    assert receipt["stopping"]["reason_code"] == PROVIDER_UNAVAILABLE
    assert LeadStatus(receipt["lead_status"]) in PERMITTED_LIVE_STATUSES
