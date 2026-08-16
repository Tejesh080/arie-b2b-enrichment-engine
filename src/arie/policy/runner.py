"""Running a policy over a set of leads and summarising what happened."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from arie.core.types import Decision
from arie.evalgen.schema import EvalLead
from arie.policy.base import Policy, PolicyOutcome, RunContext

ContextFactory = Callable[[], RunContext]


@dataclass(frozen=True)
class LeadRecord:
    """One lead's outcome, retained so regret can be computed pairwise later."""

    eval_lead_id: str
    difficulty_band: str
    value_tier: str
    oracle_decision: str
    decision: str
    correct: bool
    confidence: float
    autonomous: bool
    cost_usd: float
    latency_ms: float
    calls_made: int
    cache_hits: int
    stop_reason: str


@dataclass(frozen=True)
class PolicySummary:
    policy: str
    n_leads: int

    decision_agreement: float
    """Share of leads whose decision matches the oracle. The quality metric."""

    mean_cost_usd: float
    mean_calls: float
    mean_latency_ms: float
    cache_hit_rate: float

    autonomous_rate: float
    autonomous_error_rate: float
    """Error rate among decisions taken without a human — the safety metric."""

    escalation_rate: float
    false_reject_rate: float
    """Rejected a lead the oracle would qualify. The expensive mistake."""

    false_accept_rate: float
    agreement_by_band: dict[str, float]
    cost_by_band: dict[str, float]
    stop_reasons: dict[str, int]
    records: tuple[LeadRecord, ...] = field(repr=False, default=())

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("records")
        return payload


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def summarise(
    policy_name: str, leads: Sequence[EvalLead], outcomes: Sequence[PolicyOutcome]
) -> PolicySummary:
    records = [
        LeadRecord(
            eval_lead_id=lead.eval_lead_id,
            difficulty_band=str(lead.difficulty_band),
            value_tier=str(lead.value_tier),
            oracle_decision=str(lead.oracle_decision),
            decision=str(outcome.decision),
            correct=outcome.decision == lead.oracle_decision,
            confidence=round(outcome.confidence, 6),
            autonomous=outcome.autonomous,
            cost_usd=outcome.cost_usd,
            latency_ms=outcome.latency_ms,
            calls_made=outcome.calls_made,
            cache_hits=outcome.cache_hits,
            stop_reason=outcome.stop_reason,
        )
        for lead, outcome in zip(leads, outcomes, strict=True)
    ]

    n = len(records)
    autonomous = [r for r in records if r.autonomous]

    bands = sorted({r.difficulty_band for r in records})
    agreement_by_band = {
        band: _rate(
            sum(1 for r in records if r.difficulty_band == band and r.correct),
            sum(1 for r in records if r.difficulty_band == band),
        )
        for band in bands
    }
    cost_by_band = {
        band: round(
            sum(r.cost_usd for r in records if r.difficulty_band == band)
            / max(1, sum(1 for r in records if r.difficulty_band == band)),
            6,
        )
        for band in bands
    }

    stop_reasons: dict[str, int] = {}
    for record in records:
        stop_reasons[record.stop_reason] = stop_reasons.get(record.stop_reason, 0) + 1

    total_lookups = sum(r.calls_made + r.cache_hits for r in records)

    return PolicySummary(
        policy=policy_name,
        n_leads=n,
        decision_agreement=_rate(sum(1 for r in records if r.correct), n),
        mean_cost_usd=round(sum(r.cost_usd for r in records) / n, 8) if n else 0.0,
        mean_calls=round(sum(r.calls_made for r in records) / n, 4) if n else 0.0,
        mean_latency_ms=round(sum(r.latency_ms for r in records) / n, 2) if n else 0.0,
        cache_hit_rate=_rate(sum(r.cache_hits for r in records), total_lookups),
        autonomous_rate=_rate(len(autonomous), n),
        autonomous_error_rate=_rate(sum(1 for r in autonomous if not r.correct), len(autonomous)),
        escalation_rate=_rate(n - len(autonomous), n),
        false_reject_rate=_rate(
            sum(
                1
                for r in records
                if r.decision == str(Decision.REJECT)
                and r.oracle_decision == str(Decision.AUTO_ROUTE)
            ),
            n,
        ),
        false_accept_rate=_rate(
            sum(
                1
                for r in records
                if r.decision == str(Decision.AUTO_ROUTE)
                and r.oracle_decision == str(Decision.REJECT)
            ),
            n,
        ),
        agreement_by_band=agreement_by_band,
        cost_by_band=cost_by_band,
        stop_reasons=dict(sorted(stop_reasons.items())),
        records=tuple(records),
    )


def evaluate_policy(
    policy: Policy, leads: Sequence[EvalLead], make_context: ContextFactory
) -> PolicySummary:
    """Run `policy` over `leads` with a fresh context.

    Leads are processed in a fixed order so cache behaviour — which depends on
    the order companies are encountered — is identical for every policy. Left to
    the caller's ordering, cache hit rates would differ between strategies and
    contaminate the cost comparison.
    """
    ordered = sorted(leads, key=lambda x: x.eval_lead_id)
    ctx = make_context()
    outcomes = [policy.run(lead, ctx) for lead in ordered]
    return summarise(policy.name, ordered, outcomes)
