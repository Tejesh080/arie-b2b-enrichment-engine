"""Live-database tests for the cost ledger and the metrics views (M1 Step 9).

The views in `0002_metrics_views.sql` had never been executed against data
before these tests were written. Two of them were wrong — see
``test_v_pipeline_metrics_does_not_count_rejected_leads_as_qualified`` and
``test_v_escalation_rate_counts_a_twice_reviewed_lead_once``, which are the
regression tests for the corrections in `0005_ingestion_ledger_tracing.sql`.
Both defects returned plausible numbers, which is exactly why nothing noticed.

**On asserting against shared, live tables.** These views aggregate every lead
in the database by day, so no test here can assert an absolute value without
being at the mercy of whatever else is in there. Each view test instead measures
the metric, makes one known change, measures again, and asserts on the
*difference* — which is both pollution-proof and a sharper statement of the
property under test.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from tests.integration.conftest import IngestCleanup

from arie.core.types import LeadStatus, ProviderStatus
from arie.ledger.pricing import UnknownModelError
from arie.ledger.store import PostgresCostLedger
from arie.statemachine.transitions import QUALIFIED

pytestmark = pytest.mark.integration

_TODAY = "date_trunc('day', now())"


def _make_lead(
    conn: psycopg.Connection, cleanup: IngestCleanup, *, status: LeadStatus = LeadStatus.NEW
) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, status) VALUES (%s, %s) RETURNING lead_id",
            ("ledger-test", str(status)),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    lead_id: uuid.UUID = row[0]
    cleanup.lead_ids.append(lead_id)
    return lead_id


def _key(cleanup: IngestCleanup, kind: str = "provider") -> str:
    key = f"test-{kind}-{uuid.uuid4().hex}"
    if kind == "provider":
        cleanup.provider_call_keys.append(key)
    else:
        cleanup.model_call_keys.append(key)
    return key


def _scalar(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


def _pipeline_today(conn: psycopg.Connection) -> tuple[int, Decimal | None]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT leads_processed, cost_per_qualified_lead "
            f"FROM v_pipeline_metrics WHERE day = {_TODAY}"
        )
        row = cur.fetchone()
    return (0, None) if row is None else (int(row[0]), row[1])


def _escalation_today(conn: psycopg.Connection) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT total, escalated FROM v_escalation_rate WHERE day = {_TODAY}")
        row = cur.fetchone()
    return (0, 0) if row is None else (int(row[0]), int(row[1]))


# ------------------------------------------------------------- provider --


def test_provider_call_rolls_up_into_v_lead_cost(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id = _make_lead(db_conn, cleanup_ingest)

    write = cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="firmographics_premium",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.090,
        latency_ms=612,
        lead_id=lead_id,
    )

    assert write.recorded is True
    assert write.cost_usd == Decimal("0.090")

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.provider_cost_usd == Decimal("0.090")
    assert cost.model_cost_usd == Decimal(0)
    assert cost.total_cost_usd == Decimal("0.090")
    assert cost.provider_calls == 1
    assert cost.cache_hits == 0
    assert cost.provider_latency_ms == 612


def test_replaying_a_call_does_not_charge_for_it_twice(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """ADR 0002's "never double-charge a provider on retry", enforced by the
    UNIQUE constraint rather than by a convention someone has to remember.

    The retry reproduces the key, gets ``recorded=False`` and the *original*
    call's id back, and the ledger total does not move.
    """
    lead_id = _make_lead(db_conn, cleanup_ingest)
    key = _key(cleanup_ingest)
    call = {
        "idempotency_key": key,
        "provider": "deep_research",
        "entity_type": "company",
        "entity_id": uuid.uuid4(),
        "status": ProviderStatus.SUCCESS,
        "cost_usd": 0.600,
        "latency_ms": 4200,
        "lead_id": lead_id,
    }

    first = cost_ledger.record_provider_call(**call)  # type: ignore[arg-type]
    second = cost_ledger.record_provider_call(**call)  # type: ignore[arg-type]

    assert first.recorded is True
    assert second.recorded is False
    assert second.call_id == first.call_id
    assert second.cost_usd == Decimal("0.600")

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.provider_cost_usd == Decimal("0.600"), "the retry must not add a second charge"
    assert cost.provider_calls == 1

    assert (
        _scalar(db_conn, "SELECT count(*) FROM provider_calls WHERE idempotency_key = %s", (key,))
        == 1
    )


def test_cache_hit_is_recorded_at_zero_cost_but_still_counted(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Mirrors ``CallLedger.record``: a hit is a zero-cost call, not a missing row.

    Dropping cache hits would make cache-hit rate unmeasurable, and that metric
    is the whole justification for the company-level evidence store.
    """
    lead_id = _make_lead(db_conn, cleanup_ingest)

    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="firmographics_premium",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.090,
        latency_ms=600,
        lead_id=lead_id,
    )
    hit = cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="firmographics_premium",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.090,  # what it would have cost; deliberately not billed
        latency_ms=600,
        lead_id=lead_id,
        cache_hit=True,
    )

    assert hit.recorded is True
    assert hit.cost_usd == Decimal(0)

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.provider_cost_usd == Decimal("0.090")
    assert cost.provider_calls == 1, "cache hits are not billable calls"
    assert cost.cache_hits == 1, "but they are still counted"


def test_v_provider_health_ignores_cache_hits(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Provider health is about the provider. A cached answer says nothing
    about whether the vendor is up, and averaging zeros into its latency would
    make a heavily-cached provider look artificially fast."""
    lead_id = _make_lead(db_conn, cleanup_ingest)
    provider = f"probe_{uuid.uuid4().hex[:10]}"

    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider=provider,
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.25,
        latency_ms=1100,
        lead_id=lead_id,
    )
    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider=provider,
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.25,
        latency_ms=1100,
        lead_id=lead_id,
        cache_hit=True,
    )

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT total_calls, success_rate, avg_cost_usd, p50_latency_ms "
            "FROM v_provider_health WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == 1, "only the billable call counts toward provider health"
    assert row[1] == Decimal(1)
    assert row[2] == Decimal("0.25")
    assert row[3] == 1100


# ---------------------------------------------------------------- model --


def test_model_call_is_priced_from_token_counts(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    lead_id = _make_lead(db_conn, cleanup_ingest)

    write = cost_ledger.record_model_call(
        model="deepseek-chat",
        purpose="buying_signal_extraction",
        prompt_tokens=1_000,
        completion_tokens=100,
        lead_id=lead_id,
        latency_ms=430,
        idempotency_key=_key(cleanup_ingest, "model"),
    )

    # 1000 * 0.27/1e6 + 100 * 1.10/1e6
    assert write.cost_usd == Decimal("0.00038")

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.model_cost_usd == Decimal("0.00038")
    assert cost.provider_cost_usd == Decimal(0)
    assert cost.total_cost_usd == Decimal("0.00038")


def test_total_cost_is_provider_plus_model(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """`v_lead_cost` LEFT JOINs two independent aggregates; a lead with both
    kinds of spend is the case where a join mistake would show up."""
    lead_id = _make_lead(db_conn, cleanup_ingest)

    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="intent_signals",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=0.250,
        latency_ms=1100,
        lead_id=lead_id,
    )
    cost_ledger.record_model_call(
        model="deepseek-reasoner",
        purpose="buying_signal_extraction",
        prompt_tokens=2_000,
        completion_tokens=1_000,
        lead_id=lead_id,
        idempotency_key=_key(cleanup_ingest, "model"),
    )

    # 2000 * 0.55/1e6 + 1000 * 2.19/1e6 = 0.0011 + 0.00219
    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.provider_cost_usd == Decimal("0.250")
    assert cost.model_cost_usd == Decimal("0.00329")
    assert cost.total_cost_usd == Decimal("0.25329")


def test_model_tier_is_taken_from_the_price_table_not_the_caller(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """`v_model_escalation` divides by tier, so a call site mislabelling a
    model would corrupt the one metric that says whether the cascade earns its
    complexity. The caller is never asked."""
    lead_id = _make_lead(db_conn, cleanup_ingest)
    key = _key(cleanup_ingest, "model")

    cost_ledger.record_model_call(
        model="deepseek-reasoner",
        purpose="probe",
        prompt_tokens=10,
        completion_tokens=10,
        lead_id=lead_id,
        escalated_from="deepseek-chat",
        idempotency_key=key,
    )

    assert _scalar(db_conn, "SELECT tier FROM model_calls WHERE idempotency_key = %s", (key,)) == (
        "strong"
    )


def test_replaying_a_model_call_does_not_charge_for_it_twice(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """`model_calls` had no idempotency key before 0005; a retried LLM call
    would have been billed twice and every cost view would have overcounted."""
    lead_id = _make_lead(db_conn, cleanup_ingest)
    key = _key(cleanup_ingest, "model")
    call = {
        "model": "deepseek-chat",
        "purpose": "buying_signal_extraction",
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "lead_id": lead_id,
        "idempotency_key": key,
    }

    first = cost_ledger.record_model_call(**call)  # type: ignore[arg-type]
    second = cost_ledger.record_model_call(**call)  # type: ignore[arg-type]

    assert first.recorded is True
    assert second.recorded is False
    assert second.call_id == first.call_id

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.model_cost_usd == Decimal("0.00038")
    assert (
        _scalar(db_conn, "SELECT count(*) FROM model_calls WHERE idempotency_key = %s", (key,)) == 1
    )


def test_unkeyed_model_calls_are_never_deduplicated(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """NULL never conflicts under a UNIQUE index — the same semantics
    `jobs.idempotency_key` already documents. Two genuinely separate calls with
    no natural key must both be billed."""
    lead_id = _make_lead(db_conn, cleanup_ingest)

    for _ in range(2):
        cost_ledger.record_model_call(
            model="deepseek-chat",
            purpose="buying_signal_extraction",
            prompt_tokens=1_000,
            completion_tokens=100,
            lead_id=lead_id,
        )

    cost = cost_ledger.lead_cost(lead_id)
    assert cost is not None
    assert cost.model_cost_usd == Decimal("0.00076"), "both calls billed"

    # No idempotency key to clean up by, so remove them via the lead.
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM model_calls WHERE lead_id = %s", (lead_id,))
    db_conn.commit()


def test_an_unpriced_model_is_rejected_before_a_row_is_written(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Recording it as free would be indistinguishable downstream from a
    genuinely cheap call — untracked spend that makes the cascade look good."""
    lead_id = _make_lead(db_conn, cleanup_ingest)

    with pytest.raises(UnknownModelError):
        cost_ledger.record_model_call(
            model="gpt-hypothetical",
            purpose="probe",
            prompt_tokens=10,
            completion_tokens=10,
            lead_id=lead_id,
        )

    assert _scalar(db_conn, "SELECT count(*) FROM model_calls WHERE lead_id = %s", (lead_id,)) == 0


# ------------------------------------------- regressions for 0005's view fixes --


def test_v_pipeline_metrics_does_not_count_rejected_leads_as_qualified(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Regression for defect 1 corrected in 0005.

    'SYNCED' is where the *reject* branch terminates
    (``DECISION_OUTCOMES["reject"] -> LeadStatus.SYNCED``). While the view
    counted it as qualified, rejecting more leads made cost-per-qualified-lead
    look better — inverting the exact failure mode 0002's own comment says the
    view exists to expose.

    A qualified lead is established first so the metric is non-NULL; otherwise
    "unchanged" would pass vacuously.
    """
    qualified = _make_lead(db_conn, cleanup_ingest, status=LeadStatus.AUTO_ROUTED)
    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="firmographics_premium",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=1.00,
        latency_ms=100,
        lead_id=qualified,
    )

    leads_before, cost_per_qualified_before = _pipeline_today(db_conn)
    assert cost_per_qualified_before is not None, "a qualified lead must make the metric computable"

    rejected = _make_lead(db_conn, cleanup_ingest, status=LeadStatus.SYNCED)
    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="deep_research",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=99.00,  # large enough that including it could not go unnoticed
        latency_ms=100,
        lead_id=rejected,
    )

    leads_after, cost_per_qualified_after = _pipeline_today(db_conn)

    # The rejected lead is definitely in the view — without this the assertion
    # below could pass simply because nothing was measured.
    assert leads_after == leads_before + 1
    assert cost_per_qualified_after == cost_per_qualified_before


def test_v_pipeline_metrics_counts_manual_reviewed_leads_as_qualified(
    cost_ledger: PostgresCostLedger, db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Regression for the M1 post-freeze audit finding: 0005's fix above
    narrowed "qualified" to `('AUTO_ROUTED','ROUTED')`, written before Step 11
    existed. A human's `action=edit` decision lands a lead on MANUAL_REVIEW
    (`arie.approval.workflow._FINAL_DECISION` -> `HUMAN_REVIEW_OUTCOMES`) — a
    genuinely qualified outcome, a human routed it — but that status was in
    neither the numerator nor the denominator, so every human-edited lead's
    spend silently vanished from the metric, inflating it exactly as the
    human-review path got used.

    Computed independently from `v_lead_cost` using
    `arie.statemachine.transitions.QUALIFIED` (not the 3 status literals
    retyped a second time) and compared against what `v_pipeline_metrics`
    actually reports — if the view's filter drifts from that vocabulary
    again, this fails on the value, not merely on "did something change".
    """
    manual_review = _make_lead(db_conn, cleanup_ingest, status=LeadStatus.MANUAL_REVIEW)
    cost_ledger.record_provider_call(
        idempotency_key=_key(cleanup_ingest),
        provider="deep_research",
        entity_type="company",
        entity_id=uuid.uuid4(),
        status=ProviderStatus.SUCCESS,
        cost_usd=Decimal("12.50"),
        latency_ms=100,
        lead_id=manual_review,
    )

    _, cost_per_qualified = _pipeline_today(db_conn)

    qualified_values = ", ".join(f"'{status}'" for status in sorted(QUALIFIED))
    expected = _scalar(
        db_conn,
        f"SELECT SUM(total_cost_usd) / NULLIF(COUNT(*), 0) FROM v_lead_cost "
        f"WHERE status IN ({qualified_values}) AND date_trunc('day', created_at) = {_TODAY}",
    )

    assert cost_per_qualified is not None, "the MANUAL_REVIEW lead must make the metric computable"
    assert expected is not None
    assert cost_per_qualified == expected


def test_v_escalation_rate_counts_a_twice_reviewed_lead_once(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """Regression for defect 2 corrected in 0005.

    The LEFT JOIN to human_reviews fans a lead out to one row per review, so
    ``COUNT(*)`` counted (lead, review) pairs. A twice-reviewed lead — a
    re-review, a disputed decision — inflated both `total` and `escalated`, and
    unequally, because a non-escalated lead contributes exactly one row.
    """
    total_before, escalated_before = _escalation_today(db_conn)

    lead_id = _make_lead(db_conn, cleanup_ingest, status=LeadStatus.AWAITING_HUMAN)
    with db_conn.cursor() as cur:
        for reviewer in ("first@example.test", "second@example.test"):
            cur.execute(
                "INSERT INTO human_reviews (lead_id, reviewer, original_decision, final_decision, "
                "responded_at) VALUES (%s, %s, 'auto_route', 'reject', now())",
                (lead_id, reviewer),
            )
    db_conn.commit()

    total_after, escalated_after = _escalation_today(db_conn)

    assert total_after == total_before + 1, "one lead, however many times it was reviewed"
    assert escalated_after == escalated_before + 1


def test_two_reviews_of_one_lead_are_two_overrides(
    db_conn: psycopg.Connection, cleanup_ingest: IngestCleanup
) -> None:
    """The other half of the same fix: `human_overrode` stays per-review.

    Counting distinct leads there would have been an over-correction — two
    reviewers overriding the same lead genuinely is two overrides.
    """
    overrode_before = _scalar(
        db_conn, f"SELECT human_overrode FROM v_escalation_rate WHERE day = {_TODAY}"
    )
    baseline = 0 if overrode_before is None else int(overrode_before)

    lead_id = _make_lead(db_conn, cleanup_ingest, status=LeadStatus.AWAITING_HUMAN)
    with db_conn.cursor() as cur:
        for reviewer in ("a@example.test", "b@example.test"):
            cur.execute(
                "INSERT INTO human_reviews (lead_id, reviewer, original_decision, final_decision, "
                "responded_at) VALUES (%s, %s, 'auto_route', 'reject', now())",
                (lead_id, reviewer),
            )
    db_conn.commit()

    overrode_after = _scalar(
        db_conn, f"SELECT human_overrode FROM v_escalation_rate WHERE day = {_TODAY}"
    )
    assert int(overrode_after) == baseline + 2


def test_lead_cost_returns_none_for_an_unknown_lead(cost_ledger: PostgresCostLedger) -> None:
    assert cost_ledger.lead_cost(uuid.uuid4()) is None
