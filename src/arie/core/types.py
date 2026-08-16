"""Core domain types.

These are deliberately pure: no I/O, no database, no framework imports. Every
type here is usable by both the offline benchmark (M0) and the live service (M1),
which is what lets the benchmark exercise the *same* policy code that runs in
production rather than a reimplementation of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

EntityType = Literal["company", "person"]


class LeadStatus(StrEnum):
    """States in the lead state machine. Transitions live in arie.statemachine."""

    NEW = "NEW"
    IDENTITY_RESOLVED = "IDENTITY_RESOLVED"
    SCORING = "SCORING"
    FETCHING_EVIDENCE = "FETCHING_EVIDENCE"
    INTEGRATING = "INTEGRATING"
    DECISION = "DECISION"
    AUTO_ROUTED = "AUTO_ROUTED"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    ROUTED = "ROUTED"
    SYNCED = "SYNCED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class Decision(StrEnum):
    """The unit of quality for this system.

    Note that the benchmark measures agreement on *these*, not on score values.
    Nobody cares whether a lead scored 82 or 87; they care whether it got routed.
    """

    AUTO_ROUTE = "auto_route"
    ESCALATE_HUMAN = "escalate_human"
    REJECT = "reject"


class ProviderStatus(StrEnum):
    SUCCESS = "success"
    MISS = "miss"  # provider has no data for this entity — not an error
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class Entity:
    """A resolved company or person that evidence attaches to."""

    entity_type: EntityType
    entity_id: UUID
    canonical_key: str  # canonical_domain for company, canonical_email for person
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    """A single observed fact, with provenance and decay.

    `confidence` is the *source-reliability* confidence, distinct from the
    decision confidence produced by arie.confidence. Conflating those two is the
    most common mistake in comparable systems; keeping them as separate types
    makes the mistake harder to make.
    """

    entity_type: EntityType
    entity_id: UUID
    field_name: str
    value: Any
    source: str
    confidence: float
    ttl_seconds: int
    fetched_at: datetime
    signal_description: str | None = None
    effect_on_score: float | None = None

    def age_seconds(self, now: datetime) -> float:
        return (now - self.fetched_at).total_seconds()

    def is_expired(self, now: datetime) -> bool:
        return self.age_seconds(now) > self.ttl_seconds

    def effective_confidence(self, now: datetime) -> float:
        """Staleness-discounted confidence.

        Cached evidence is not free-and-perfect, nor is it worthless — it decays.
        This is what lets zero-cost cached facts compete honestly against a fresh
        paid call inside the EVoI calculation.
        """
        if self.ttl_seconds <= 0:
            return 0.0
        decay = max(0.0, 1.0 - (self.age_seconds(now) / self.ttl_seconds))
        return self.confidence * decay


@dataclass(frozen=True)
class ProviderResult:
    """What every provider returns — simulated or real, identically."""

    fields: dict[str, Any]
    confidence: float
    cost_usd: float
    latency_ms: float
    status: ProviderStatus
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreBreakdown:
    """Deterministic score plus the per-component attribution that produced it.

    Storing the breakdown rather than just the total is what makes every decision
    reconstructable — the auditability requirement.
    """

    total_score: float
    components: dict[str, float]
    model_version: str


@dataclass(frozen=True)
class VoIEvaluation:
    """One candidate provider's expected value of information.

    EVoI = P(flips decision) * business_value - cost - latency_penalty

    Every one of these is persisted (voi_decisions table), including the ones we
    *rejected*. The rejected rows are the audit trail for why the system stopped.
    """

    candidate_provider: str
    p_flips_decision: float
    business_value: float
    expected_cost: float
    latency_penalty: float

    @property
    def net_evoi(self) -> float:
        return (
            self.p_flips_decision * self.business_value - self.expected_cost - self.latency_penalty
        )


@dataclass(frozen=True)
class DecisionOutcome:
    """Terminal output of the policy loop for one lead."""

    decision: Decision
    score: ScoreBreakdown
    decision_confidence: float
    evidence_used: tuple[Evidence, ...]
    voi_trace: tuple[VoIEvaluation, ...]
    total_cost_usd: float
    total_latency_ms: float
    provider_calls_made: int
    cache_hits: int
    stop_reason: str
