"""Quarantine: fixtures leave the aggregates, and keep their audit trail.

A production audit of one organization found 194 leads of which exactly one
was real customer data. The other 193 — integration harnesses, canaries, a
load exercise, frozen-corpus identities, reserved-domain fixtures — were
counted in every number the product showed: the dashboard, a 70-item review
queue, $40.89 of "spend" priced from a benchmark assumption, and an Ask ARIE
answer that was 16 fixtures out of 20.

`leads.data_class` (migration 0042) makes provenance a fact the row carries.
These tests hold both halves of the deal: a non-production lead is invisible
to every aggregate, and completely visible by id.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from arie.api.main import AppState, create_app, get_auth_context
from arie.auth import AuthContext
from arie.copilot import CopilotIntent, SortOption
from arie.copilot_service import _fetch_lead_pool, _select_candidates, _sort_summaries
from arie.data_class import LeadDataClass
from arie.icp_profiles import resolve_scoring_config
from arie.ledger.cost_basis import CostBasis
from arie.limits import get_usage_against_limits
from arie.tenancy import LEGACY_ORGANIZATION_ID as ORG
from arie.usage import get_usage_summary

pytestmark = pytest.mark.integration

_PREFIX = f"dc{uuid.uuid4().hex[:8]}"


def _domain(label: str) -> str:
    return f"{_PREFIX}-{label}.test"


SEEDED = {
    LeadDataClass.PRODUCTION: _domain("production"),
    LeadDataClass.UNKNOWN: _domain("unknown"),
    LeadDataClass.BENCHMARK: _domain("benchmark"),
    LeadDataClass.INTEGRATION_TEST: _domain("integration"),
    LeadDataClass.LOAD_TEST: _domain("load"),
}


def _seed(
    conn: psycopg.Connection,
    *,
    data_class: LeadDataClass,
    domain: str,
    cost_basis: str,
    cost_usd: str,
    actual_cost_usd: str | None,
) -> UUID:
    company_id, person_id, lead_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO companies (company_id, canonical_domain, name) VALUES (%s,%s,%s)",
            (company_id, domain, domain),
        )
        cur.execute(
            "INSERT INTO persons (person_id, organization_id, company_id, canonical_email,"
            " full_name) VALUES (%s,%s,%s,%s,%s)",
            (person_id, ORG, company_id, f"contact@{domain}", "Test Contact"),
        )
        cur.execute(
            "INSERT INTO leads (lead_id, organization_id, person_id, company_id, source,"
            " status, data_class) VALUES (%s,%s,%s,%s,'data-class-it','AUTO_ROUTED',%s)",
            (lead_id, ORG, person_id, company_id, str(data_class)),
        )
        cur.execute(
            "INSERT INTO decision_receipts (lead_id, organization_id, decision, autonomous,"
            " confidence, tau, score_value, score_lower, score_upper, stop_reason,"
            " policy_name, scorer_version, confidence_calibration, evidence_snapshot)"
            " VALUES (%s,%s,'auto_route',true,0.91,0.80,80.0,80.0,80.0,'decision_settled',"
            "'calibrated_bounds','s','c',%s)",
            (
                lead_id,
                ORG,
                psycopg.types.json.Jsonb(
                    {
                        "known": [
                            {
                                "field": "industry",
                                "source": "abstract",
                                "confidence": 0.9,
                                "contested": False,
                            }
                        ],
                        "unknown": [],
                    }
                ),
            ),
        )
        cur.execute(
            "INSERT INTO provider_calls (lead_id, organization_id, provider, entity_type,"
            " entity_id, idempotency_key, status, cost_usd, cache_hit, cost_basis,"
            " actual_cost_usd) VALUES (%s,%s,'abstract','company',%s,%s,'ok',%s,false,%s,%s)",
            (
                lead_id,
                ORG,
                company_id,
                f"{_PREFIX}-{uuid.uuid4()}",
                cost_usd,
                cost_basis,
                actual_cost_usd,
            ),
        )
        cur.execute(
            "INSERT INTO human_reviews (lead_id, organization_id, original_decision)"
            " VALUES (%s,%s,'escalate_human')",
            (lead_id, ORG),
        )
    conn.commit()
    return lead_id


@pytest.fixture
def seeded(db_conn: psycopg.Connection) -> Iterator[dict[LeadDataClass, UUID]]:
    """One lead per class. The production one is priced as a real vendor call
    that consumed a free credit; every other one is simulated."""
    _purge(db_conn)
    ids = {
        LeadDataClass.PRODUCTION: _seed(
            db_conn,
            data_class=LeadDataClass.PRODUCTION,
            domain=SEEDED[LeadDataClass.PRODUCTION],
            cost_basis=str(CostBasis.MODELLED_CREDIT_EQUIVALENT),
            cost_usd="0.0049",
            actual_cost_usd="0.0000",
        )
    }
    for klass in (
        LeadDataClass.UNKNOWN,
        LeadDataClass.BENCHMARK,
        LeadDataClass.INTEGRATION_TEST,
        LeadDataClass.LOAD_TEST,
    ):
        ids[klass] = _seed(
            db_conn,
            data_class=klass,
            domain=SEEDED[klass],
            cost_basis=str(CostBasis.SIMULATED_CATALOGUE),
            cost_usd="1.0078",
            actual_cost_usd=None,
        )
    try:
        yield ids
    finally:
        _purge(db_conn)


def _purge(conn: psycopg.Connection) -> None:
    """By domain, so a run that dies before teardown cannot poison the next."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT lead_id FROM leads l JOIN companies c ON c.company_id = l.company_id"
            " WHERE c.canonical_domain = ANY(%s)",
            (list(SEEDED.values()),),
        )
        lead_ids = [r[0] for r in cur.fetchall()]
        for table in ("human_reviews", "provider_calls", "decision_receipts", "lead_events"):
            cur.execute(f"DELETE FROM {table} WHERE lead_id = ANY(%s)", (lead_ids,))
        cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
        cur.execute(
            "DELETE FROM persons WHERE company_id IN "
            "(SELECT company_id FROM companies WHERE canonical_domain = ANY(%s))",
            (list(SEEDED.values()),),
        )
        cur.execute(
            "DELETE FROM companies WHERE canonical_domain = ANY(%s)", (list(SEEDED.values()),)
        )
    conn.commit()


def _client(app_state: AppState) -> TestClient:
    app = create_app(state=app_state)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        organization_id=ORG, auth_method="jwt", user_id=uuid.uuid4(), role="owner"
    )
    return TestClient(app, raise_server_exceptions=False)


# --- the aggregates see production, and only production ----------------------


def _our_pool_companies(db_conn: psycopg.Connection) -> list[str]:
    """The seeded companies visible in the bounded lead pool that the
    dashboard (priority counts, Top Leads, its review figure) and Ask ARIE
    both read -- one pool, so one filter covers every one of them."""
    config = resolve_scoring_config(db_conn, organization_id=ORG)
    pool = _fetch_lead_pool(
        db_conn,
        organization_id=ORG,
        user_id=uuid.uuid4(),
        threshold_qualify=config.qualify_threshold,
        threshold_reject=config.reject_threshold,
    )
    return [
        str(row.summary.company)
        for row in pool
        if str(row.summary.company or "").startswith(_PREFIX)
    ]


def test_the_dashboard_pool_shows_production_and_nothing_else(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    """All five leads are decided and have a pending `human_reviews` row. The
    audited organization's dashboard counted a 70-item review queue that
    contained zero real leads."""
    assert _our_pool_companies(db_conn) == [SEEDED[LeadDataClass.PRODUCTION]]


def test_ask_arie_sees_only_production_leads(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    config = resolve_scoring_config(db_conn, organization_id=ORG)
    pool = _fetch_lead_pool(
        db_conn,
        organization_id=ORG,
        user_id=uuid.uuid4(),
        threshold_qualify=config.qualify_threshold,
        threshold_reject=config.reject_threshold,
    )
    answer = _sort_summaries(
        _select_candidates(CopilotIntent.TOP_LEADS, pool, scoring_config=None), SortOption.PRIORITY
    )

    ours = [s for s in answer if str(s.company or "").startswith(_PREFIX)]
    assert [s.company for s in ours] == [SEEDED[LeadDataClass.PRODUCTION]]


def test_usage_counts_only_production_leads(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    now = datetime.now(UTC)
    summary = get_usage_summary(
        db_conn, organization_id=ORG, from_at=now.replace(hour=0, minute=0), to_at=now
    )

    assert summary.leads_processed >= 1
    # The four fixtures seeded in this window must not be among them.
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM leads WHERE organization_id = %s AND created_at >= %s"
            " AND data_class <> 'production'",
            (ORG, now.replace(hour=0, minute=0)),
        )
        row = cur.fetchone()
    assert row is not None and row[0] >= 4


@pytest.mark.parametrize(
    "klass",
    [
        LeadDataClass.UNKNOWN,
        LeadDataClass.BENCHMARK,
        LeadDataClass.INTEGRATION_TEST,
        LeadDataClass.LOAD_TEST,
    ],
)
def test_a_non_production_lead_is_absent_from_every_aggregate(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID], klass: LeadDataClass
) -> None:
    assert SEEDED[klass] not in _our_pool_companies(db_conn)


# --- and keeps its audit trail ------------------------------------------------


@pytest.mark.parametrize("klass", list(SEEDED))
def test_every_class_stays_openable_by_id(
    app_state: AppState, seeded: dict[LeadDataClass, UUID], klass: LeadDataClass
) -> None:
    """Quarantine is not deletion. A benchmark lead's own page still loads."""
    with _client(app_state) as client:
        response = client.get(f"/leads/{seeded[klass]}")

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("klass", list(SEEDED))
def test_every_class_keeps_its_receipt(
    app_state: AppState, seeded: dict[LeadDataClass, UUID], klass: LeadDataClass
) -> None:
    with _client(app_state) as client:
        response = client.get(f"/leads/{seeded[klass]}/receipt")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "decided"
    assert body["decision"]["recommended_action"] == "auto_route"


# --- money is money, and modelled cost is not --------------------------------


def test_simulated_spend_never_consumes_the_monetary_allowance(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    """Four fixture rows at the simulated catalogue's $1.0078 apiece. The
    audited organization had burned a $50/month ceiling exactly this way."""
    now = datetime.now(UTC)
    usage = get_usage_against_limits(db_conn, organization_id=ORG, now=now)

    assert usage.modelled_spend_usd >= 4 * 1.0078
    assert usage.actual_spend_usd < 0.01
    # The allowance reads estimated live usage, and the four fixtures
    # contribute nothing to it: simulated cost is not live-service usage.
    assert usage.estimated_live_spend_used_usd == usage.estimated_live_cost_usd
    assert usage.estimated_live_cost_usd < usage.modelled_spend_usd


def test_a_free_credit_call_is_real_and_costs_nothing(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    """The production lead's call is `modelled_credit_equivalent` with
    `actual_cost_usd = 0`: a real vendor call that drew down a pre-paid
    credit. It counts as a real call and adds nothing to money."""
    now = datetime.now(UTC)
    summary = get_usage_summary(
        db_conn, organization_id=ORG, from_at=now.replace(hour=0, minute=0), to_at=now
    )

    assert summary.real_provider_calls >= 1
    assert summary.estimated_live_cost_usd >= 0.0049
    assert summary.actual_spend_usd == pytest.approx(0.0, abs=1e-9)


def test_a_billed_call_does_increment_actual_spend(db_conn: psycopg.Connection) -> None:
    """The other half: when a vendor reports a figure, it is money."""
    domain = _domain("billed")
    SEEDED[LeadDataClass.QUARANTINED] = domain
    try:
        _seed(
            db_conn,
            data_class=LeadDataClass.PRODUCTION,
            domain=domain,
            cost_basis=str(CostBasis.VENDOR_BILLED),
            cost_usd="0.2500",
            actual_cost_usd="0.2500",
        )
        now = datetime.now(UTC)
        summary = get_usage_summary(
            db_conn, organization_id=ORG, from_at=now.replace(hour=0, minute=0), to_at=now
        )

        assert summary.actual_spend_usd >= 0.25
    finally:
        _purge(db_conn)
        SEEDED.pop(LeadDataClass.QUARANTINED, None)


def test_the_simulated_worker_path_records_an_explicit_basis(
    db_conn: psycopg.Connection,
) -> None:
    """Every billable row the *worker* writes says what its cost is.

    Scoped to `idempotency_key LIKE 'job:%'`, which is
    `arie.jobs.handlers`'s own simulated ledger adapter and nothing else. The
    table as a whole still holds rows other tests INSERT directly with no
    basis, and asserting over all of them would be a claim about test
    fixtures rather than about production code.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM provider_calls"
            " WHERE idempotency_key LIKE 'job:%%'"
            "   AND cache_hit = false AND cost_basis IS NULL"
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == 0, "a billable row with no cost basis is a number nobody can classify"


def test_the_cost_basis_vocabulary_is_enforced_by_the_database(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO provider_calls (lead_id, organization_id, provider, entity_type,"
            " entity_id, idempotency_key, status, cost_usd, cache_hit, cost_basis)"
            " VALUES (NULL,%s,'x','company',%s,%s,'ok',0,false,'free_lunch')",
            (ORG, uuid.uuid4(), f"{_PREFIX}-bad"),
        )
    db_conn.rollback()


def test_the_data_class_vocabulary_is_enforced_by_the_database(
    db_conn: psycopg.Connection,
) -> None:
    """A typo'd class would silently quarantine a real customer's leads."""
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO leads (lead_id, organization_id, source, status, data_class)"
            " VALUES (%s,%s,'x','NEW','prod')",
            (uuid.uuid4(), ORG),
        )
    db_conn.rollback()


# --- a harness cannot create production data by accident ---------------------


def _api_key_client(app_state: AppState) -> TestClient:
    app = create_app(state=app_state)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        organization_id=ORG,
        auth_method="api_key",
        api_key_id=uuid.uuid4(),
        scopes=frozenset({"leads:write"}),
    )
    return TestClient(app, raise_server_exceptions=False)


def _ingest(client: TestClient, domain: str, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return client.post(
        "/leads",
        json={
            "source": "data-class-it",
            "email": f"contact@{domain}",
            "external_ref": f"{_PREFIX}-{uuid.uuid4().hex[:8]}",
            "company_domain": domain,
        },
        headers=headers or {},
    )


def _class_of(conn: psycopg.Connection, lead_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT data_class FROM leads WHERE lead_id = %s", (lead_id,))
        row = cur.fetchone()
    assert row is not None
    return str(row[0])


def test_ordinary_ingest_creates_production(
    app_state: AppState, db_conn: psycopg.Connection, pool: ConnectionPool | None = None
) -> None:
    domain = _domain("plain")
    SEEDED[LeadDataClass.QUARANTINED] = domain
    try:
        with _client(app_state) as client:
            response = _ingest(client, domain)
        assert response.status_code == 201, response.text
        assert _class_of(db_conn, response.json()["lead_id"]) == "production"
    finally:
        _purge(db_conn)
        SEEDED.pop(LeadDataClass.QUARANTINED, None)


def test_a_machine_credential_can_mark_its_own_writes_as_test_data(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    domain = _domain("marked")
    SEEDED[LeadDataClass.QUARANTINED] = domain
    try:
        with _api_key_client(app_state) as client:
            response = _ingest(client, domain, {"X-ARIE-Data-Class": "integration_test"})
        assert response.status_code == 201, response.text
        assert _class_of(db_conn, response.json()["lead_id"]) == "integration_test"
    finally:
        _purge(db_conn)
        SEEDED.pop(LeadDataClass.QUARANTINED, None)


def test_nothing_can_claim_to_be_production(app_state: AppState) -> None:
    """The one-way rule. A harness may downgrade its own data and may never
    assert a customer's."""
    with _api_key_client(app_state) as client:
        response = _ingest(client, _domain("claim"), {"X-ARIE-Data-Class": "production"})

    assert response.status_code == 422, response.text
    assert "cannot be requested" in response.json()["detail"]


def test_a_signed_in_session_cannot_reclassify_anything(app_state: AppState) -> None:
    with _client(app_state) as client:
        response = _ingest(client, _domain("session"), {"X-ARIE-Data-Class": "load_test"})

    assert response.status_code == 403, response.text
    assert "API key" in response.json()["detail"]


def test_an_unknown_class_is_refused_rather_than_ignored(app_state: AppState) -> None:
    """Silently ignoring it would create production data from a harness that
    believed it had marked itself."""
    with _api_key_client(app_state) as client:
        response = _ingest(client, _domain("bogus"), {"X-ARIE-Data-Class": "staging"})

    assert response.status_code == 422, response.text


def test_the_production_index_covers_the_product_predicate(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_leads_org_production'")
        row = cur.fetchone()

    assert row is not None, "the partial index migration 0042 creates is missing"
    assert "data_class = 'production'" in row[0]


def test_decimal_costs_survive_the_split(db_conn: psycopg.Connection) -> None:
    """Guards the float() conversions in `get_usage_summary` against a NUMERIC
    coming back as Decimal and silently becoming a string."""
    now = datetime.now(UTC)
    summary = get_usage_summary(
        db_conn, organization_id=ORG, from_at=now.replace(hour=0, minute=0), to_at=now
    )

    for value in (
        summary.modelled_spend_usd,
        summary.estimated_live_cost_usd,
        summary.actual_spend_usd,
    ):
        assert isinstance(value, float)
        assert not isinstance(value, Decimal)


def test_the_test_database_defaults_to_a_harness_class(db_conn: psycopg.Connection) -> None:
    """The structural half of harness hygiene.

    Production defaults `leads.data_class` to `production`. This database
    defaults it to `integration_test`, so a seed helper that forgets the column
    cannot put fixtures into a surface a customer reads. Set once per session
    by `tests.integration.conftest.harness_data_class_default`.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = 'leads' AND column_name = 'data_class'"
        )
        row = cur.fetchone()

    assert row is not None
    assert "integration_test" in str(row[0])


# --- the operational allowance gates work, not invoices ----------------------


@pytest.fixture(autouse=True)
def _purge_seeded_model_calls(db_conn: psycopg.Connection) -> Iterator[None]:
    """Remove this module's own `model_calls` rows after every test.

    They are ledger rows like any other, so leaving them behind inflates the
    organization's month-to-date LLM spend and eventually trips
    `arie.llm.budget`'s monthly ceiling for every *other* test in the suite —
    which is exactly what happened while writing these.
    """
    yield
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM model_calls WHERE idempotency_key LIKE %s", (f"{_PREFIX}-model-%",)
        )
    db_conn.commit()


def _seed_model_call(
    conn: psycopg.Connection, *, cost_usd: str, basis: str, actual: str | None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_calls (organization_id, model, tier, purpose,"
            " prompt_tokens, completion_tokens, cost_usd, idempotency_key,"
            " provider, actual_cost_usd, cost_basis)"
            " VALUES (%s,'deepseek-chat','cheap','copilot',100,10,%s,%s,'deepseek',%s,%s)",
            (ORG, cost_usd, f"{_PREFIX}-model-{uuid.uuid4()}", actual, basis),
        )
    conn.commit()


def _today(conn: psycopg.Connection):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    return get_usage_summary(
        conn, organization_id=ORG, from_at=now.replace(hour=0, minute=0), to_at=now
    )


def test_a_real_model_call_is_live_usage_not_modelled_spend(
    db_conn: psycopg.Connection,
) -> None:
    """DeepSeek and Bedrock report token counts, never dollars, so their cost
    is a list-price estimate of real work — `estimated_live`, not `modelled`
    and not `actual`."""
    before = _today(db_conn)
    _seed_model_call(db_conn, cost_usd="0.0500", basis="modelled_list_price", actual=None)
    after = _today(db_conn)

    assert after.estimated_live_cost_usd == pytest.approx(
        before.estimated_live_cost_usd + 0.05, abs=1e-6
    )
    assert after.modelled_spend_usd == pytest.approx(before.modelled_spend_usd, abs=1e-9)
    assert after.actual_spend_usd == pytest.approx(before.actual_spend_usd, abs=1e-9)
    assert after.real_model_calls == before.real_model_calls + 1


def test_a_fake_model_call_is_modelled_spend_only(db_conn: psycopg.Connection) -> None:
    before = _today(db_conn)
    _seed_model_call(db_conn, cost_usd="0.0000", basis="simulated_catalogue", actual=None)
    after = _today(db_conn)

    assert after.estimated_live_cost_usd == pytest.approx(before.estimated_live_cost_usd, abs=1e-9)
    assert after.real_model_calls == before.real_model_calls


def test_a_free_credit_real_call_still_consumes_the_live_allowance(
    db_conn: psycopg.Connection,
) -> None:
    """The case a money-based guard gets wrong. `actual_cost_usd = 0` is the
    truth — a pre-paid credit funded it — and the request still went out, so
    the operational allowance must still shrink."""
    now = datetime.now(UTC)
    before = get_usage_against_limits(db_conn, organization_id=ORG, now=now)
    _seed_model_call(db_conn, cost_usd="0.0300", basis="modelled_list_price", actual="0.0000")
    after = get_usage_against_limits(db_conn, organization_id=ORG, now=now)

    assert after.estimated_live_spend_used_usd > before.estimated_live_spend_used_usd
    assert after.estimated_live_spend_remaining_usd < before.estimated_live_spend_remaining_usd
    assert after.actual_spend_usd == pytest.approx(before.actual_spend_usd, abs=1e-9)


def test_a_billed_call_moves_both_reporting_and_the_allowance(
    db_conn: psycopg.Connection,
) -> None:
    now = datetime.now(UTC)
    before = get_usage_against_limits(db_conn, organization_id=ORG, now=now)
    _seed_model_call(db_conn, cost_usd="0.2000", basis="vendor_billed", actual="0.2000")
    after = get_usage_against_limits(db_conn, organization_id=ORG, now=now)

    assert after.actual_spend_usd == pytest.approx(before.actual_spend_usd + 0.2, abs=1e-6)
    assert after.estimated_live_spend_used_usd > before.estimated_live_spend_used_usd


def test_simulated_work_can_never_shrink_the_live_allowance(
    db_conn: psycopg.Connection, seeded: dict[LeadDataClass, UUID]
) -> None:
    """Four fixtures at the simulated catalogue's $1.0078 apiece. Before this
    change that was $4.03 off a $50/month ceiling."""
    now = datetime.now(UTC)
    before = get_usage_against_limits(db_conn, organization_id=ORG, now=now)
    _seed_model_call(db_conn, cost_usd="0.9900", basis="simulated_catalogue", actual=None)
    after = get_usage_against_limits(db_conn, organization_id=ORG, now=now)

    assert after.modelled_spend_usd > before.modelled_spend_usd
    assert after.estimated_live_spend_remaining_usd == pytest.approx(
        before.estimated_live_spend_remaining_usd, abs=1e-9
    )


def test_the_model_cost_basis_vocabulary_is_enforced(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO model_calls (organization_id, model, tier, purpose,"
            " prompt_tokens, completion_tokens, cost_usd, provider, cost_basis)"
            " VALUES (%s,'deepseek-chat','cheap','copilot',1,1,0,'deepseek','guesswork')",
            (ORG,),
        )
    db_conn.rollback()


# --- batches: a harness's upload is not the customer's -----------------------


def test_a_harness_only_batch_is_left_out_of_the_listing_but_still_opens(
    db_conn: psycopg.Connection,
) -> None:
    """The audited organization's only batch was `m3_rollout_smoketest.csv`,
    and the dashboard's "Recent batch" card showed it. A batch whose every lead
    is non-production is left out of the listing; one with any production lead,
    and one whose rows were all rejected (no leads at all), stay listed. Every
    one of them still opens by id."""
    from arie.batches import get_batch, list_batches

    user_id = uuid.uuid4()
    harness, customer, all_rejected = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lead_ids: list[UUID] = []
    with db_conn.cursor() as cur:
        for batch_id, name in (
            (harness, "harness"),
            (customer, "customer"),
            (all_rejected, "rejected"),
        ):
            cur.execute(
                "INSERT INTO lead_batches (batch_id, organization_id, filename, total_rows,"
                " accepted_rows, rejected_rows, created_by_user_id)"
                " VALUES (%s, %s, %s, 2, 0, 2, %s)",
                (batch_id, ORG, f"{_PREFIX}-{name}.csv", user_id),
            )
        for batch_id, klass in (
            (harness, LeadDataClass.INTEGRATION_TEST),
            (harness, LeadDataClass.BENCHMARK),
            (customer, LeadDataClass.PRODUCTION),
            (customer, LeadDataClass.INTEGRATION_TEST),
        ):
            lead_id = uuid.uuid4()
            lead_ids.append(lead_id)
            cur.execute(
                "INSERT INTO leads (lead_id, organization_id, source, status, batch_id,"
                " data_class) VALUES (%s, %s, 'data-class-it', 'NEW', %s, %s)",
                (lead_id, ORG, batch_id, str(klass)),
            )
    db_conn.commit()
    try:
        listed = {
            b.batch_id
            for b in list_batches(db_conn, organization_id=ORG, limit=10_000)
            if b.filename.startswith(_PREFIX)
        }
        assert listed == {customer, all_rejected}
        for batch_id in (harness, customer, all_rejected):
            assert get_batch(db_conn, organization_id=ORG, batch_id=batch_id) is not None
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE lead_id = ANY(%s)", (lead_ids,))
            cur.execute(
                "DELETE FROM lead_batches WHERE batch_id = ANY(%s)",
                ([harness, customer, all_rejected],),
            )
        db_conn.commit()
