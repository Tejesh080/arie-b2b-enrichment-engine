"""Pure parts of the Decision Receipt — no database.

The database-backed composition (`arie.api.receipt.build_receipt`) is covered by
`tests/integration/test_receipt_integration.py`; this module covers what can be
tested without one: the stop-reason vocabulary and the evidence snapshot the
handler freezes at decision time.
"""

from __future__ import annotations

from arie.api.receipt import _STOP_REASON_EXPLANATIONS, stop_reason_explanation
from arie.core.types import Decision
from arie.jobs.handlers import _evidence_snapshot
from arie.policy.base import PolicyOutcome
from arie.scoring.engine import ScoringResult
from arie.scoring.merge import FieldResolution
from arie.scoring.rules import RULES_VERSION, score_facts


def test_every_real_stop_reason_has_an_explanation() -> None:
    """`CalibratedBoundsPolicy.run` only ever sets one of these three — see
    arie.policy.production. A reason code with no explanation would surface as a
    confusing fallback string on a real receipt."""
    for code in ("decision_settled", "confidence_reached", "all_providers_called"):
        assert code in _STOP_REASON_EXPLANATIONS
        assert stop_reason_explanation(code)


def test_unknown_stop_reason_falls_back_instead_of_raising() -> None:
    """Forward-compatible: a reason code this module doesn't recognise yet must
    not crash the receipt endpoint."""
    explanation = stop_reason_explanation("some_future_reason")
    assert "some_future_reason" in explanation


def test_settled_explanation_does_not_overclaim_certainty() -> None:
    """`docs/06-m1-handoff.md`'s "five things most likely to be got wrong" #1:
    is_settled means "nothing left worth buying", not "certainly correct" — the
    explanation must say so rather than implying the decision is proven right."""
    settled = _STOP_REASON_EXPLANATIONS["decision_settled"].lower()
    assert "not certainty" in settled or "not certain" in settled


def _scoring_result(resolutions: dict[str, FieldResolution]) -> ScoringResult:
    facts = {name: r.value for name, r in resolutions.items()}
    breakdown = score_facts(facts)
    from arie.scoring.engine import compute_bounds, compute_signals

    return ScoringResult(
        breakdown=breakdown,
        decision=Decision.ESCALATE_HUMAN,
        bounds=compute_bounds(facts),
        signals=compute_signals(facts, resolutions),
        facts=facts,
        resolutions=resolutions,
    )


def _outcome(resolutions: dict[str, FieldResolution]) -> PolicyOutcome:
    return PolicyOutcome(
        decision=Decision.ESCALATE_HUMAN,
        confidence=0.5,
        autonomous=False,
        providers_called=("internal_crm",),
        cost_usd=0.0,
        latency_ms=0.0,
        cache_hits=0,
        stop_reason="all_providers_called",
        scoring=_scoring_result(resolutions),
    )


def test_evidence_snapshot_records_the_winning_source_per_field() -> None:
    resolutions = {
        "industry": FieldResolution(
            field_name="industry",
            value="software",
            source="internal_crm",
            confidence=0.9,
            candidate_count=1,
            conflict_points=0.0,
        ),
    }
    snapshot = _evidence_snapshot(_outcome(resolutions).scoring)

    assert snapshot["known"] == [
        {
            "field": "industry",
            "source": "internal_crm",
            "confidence": 0.9,
            "candidate_count": 1,
            "contested": False,
        }
    ]


def test_evidence_snapshot_flags_contested_fields() -> None:
    resolutions = {
        "employee_count": FieldResolution(
            field_name="employee_count",
            value=180,
            source="firmographics_premium",
            confidence=0.95,
            candidate_count=2,
            conflict_points=10.0,  # > CONFLICT_EPSILON
        ),
    }
    snapshot = _evidence_snapshot(_outcome(resolutions).scoring)

    assert snapshot["known"][0]["contested"] is True


def test_evidence_snapshot_lists_unknown_fields() -> None:
    """Every SCORED_FIELDS entry not resolved is reported as unknown — what
    ARIE decided it didn't need to find out, not silently dropped."""
    snapshot = _evidence_snapshot(_outcome({}).scoring)
    assert "buying_intent" in snapshot["unknown"]
    assert "disqualifying_flag" in snapshot["unknown"]
    assert snapshot["known"] == []


def test_evidence_snapshot_is_json_serializable() -> None:
    """Written via psycopg's Jsonb wrapper — anything not JSON-plain here would
    fail at insert time, not at test time, which is a worse place to find out."""
    import json

    resolutions = {
        "industry": FieldResolution(
            field_name="industry",
            value="software",
            source="internal_crm",
            confidence=0.9,
            candidate_count=1,
            conflict_points=0.0,
        ),
    }
    snapshot = _evidence_snapshot(_outcome(resolutions).scoring)
    json.dumps(snapshot)  # must not raise


def test_scorer_version_constant_is_what_receipts_will_report() -> None:
    """Pins the assumption `arie.api.receipt`'s docstring and the handler both
    make: `scores.model_version` / `decision_receipts.scorer_version` is
    `arie.scoring.rules.RULES_VERSION`."""
    assert score_facts({}).model_version == RULES_VERSION
