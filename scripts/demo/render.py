"""Turn a raw Decision Receipt (a plain dict, as returned by ARIE's HTTP API)
into presentation-ready values — concise, human-readable, and never claiming
more than the receipt actually supports.

Kept pure and dict-in/dataclass-out so it can be unit tested without a running
stack or a database: feed it a captured receipt body, assert on the rendered
result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderCall:
    provider: str
    status: str
    cost_usd: str
    latency_ms: int | None


@dataclass(frozen=True)
class ProviderActivity:
    """Splits `providers.called` on `cache_hit` — a cache reuse must never be
    presented as a fresh external request, and `not_called` is reported as a
    plain set difference, not a claim that each one was individually
    evaluated and rejected."""

    fresh: tuple[ProviderCall, ...]
    cache_reused: tuple[ProviderCall, ...]
    not_called: tuple[str, ...]

    @property
    def fresh_count(self) -> int:
        return len(self.fresh)

    @property
    def cache_reused_count(self) -> int:
        return len(self.cache_reused)


@dataclass(frozen=True)
class HumanReviewRendering:
    review_id: str
    required: bool
    reviewer: str | None
    original_decision: str | None
    action: str | None
    final_decision: str | None
    responded_at: str | None
    is_override: bool


@dataclass(frozen=True)
class RenderedDecision:
    """One lead's receipt, translated into what the report shows. Every field
    is read straight off the receipt — nothing here recomputes a score, a
    threshold, or a confidence value."""

    label: str
    """A short human identifier, e.g. "Nadia Delacroix — Lumen500 Ltd"."""
    lead_id: str
    status: str
    lead_status: str

    recommended_action: str | None
    final_status: str | None
    autonomous: bool | None
    human_override: bool

    score_value: float | None
    score_lower: float | None
    score_upper: float | None
    threshold_qualify: float | None
    threshold_reject: float | None
    confidence: float | None
    tau: float | None

    stop_reason: str | None
    stop_explanation: str | None

    provider_cost_usd: str
    model_cost_usd: str
    total_cost_usd: str

    evidence_known: tuple[str, ...]
    evidence_unknown: tuple[str, ...]
    activity: ProviderActivity

    policy_version: str | None
    scorer_version: str | None
    confidence_calibration: str | None

    human_review: HumanReviewRendering | None


def split_provider_activity(receipt: dict[str, Any]) -> ProviderActivity:
    """`providers.called` mixes fresh calls and cache hits under one list —
    split on `cache_hit` so the report never implies a cached result was a new
    external request."""
    providers = receipt.get("providers") or {}
    called = providers.get("called") or []
    fresh = tuple(
        ProviderCall(
            provider=c["provider"],
            status=c["status"],
            cost_usd=str(c["cost_usd"]),
            latency_ms=c.get("latency_ms"),
        )
        for c in called
        if not c["cache_hit"]
    )
    cache_reused = tuple(
        ProviderCall(
            provider=c["provider"],
            status=c["status"],
            cost_usd=str(c["cost_usd"]),
            latency_ms=c.get("latency_ms"),
        )
        for c in called
        if c["cache_hit"]
    )
    not_called = tuple(providers.get("not_called") or ())
    return ProviderActivity(fresh=fresh, cache_reused=cache_reused, not_called=not_called)


def _human_review(receipt: dict[str, Any]) -> HumanReviewRendering | None:
    hr = receipt.get("human_review")
    if hr is None:
        return None
    decision = receipt.get("decision") or {}
    return HumanReviewRendering(
        review_id=str(hr["review_id"]),
        required=hr["required"],
        reviewer=hr.get("reviewer"),
        original_decision=hr.get("original_decision"),
        action=hr.get("action"),
        final_decision=hr.get("final_decision"),
        responded_at=hr.get("responded_at"),
        is_override=bool(decision.get("human_override", False)),
    )


def render_decision(label: str, receipt: dict[str, Any]) -> RenderedDecision:
    """The one place a raw receipt dict becomes something the report renders."""
    decision = receipt.get("decision")
    score = receipt.get("score")
    stopping = receipt.get("stopping")
    versions = receipt.get("versions")
    cost = receipt.get("cost") or {}
    evidence = receipt.get("evidence") or {}
    bounds = (score or {}).get("bounds") or {}

    return RenderedDecision(
        label=label,
        lead_id=str(receipt["lead_id"]),
        status=str(receipt["status"]),
        lead_status=str(receipt["lead_status"]),
        recommended_action=decision.get("recommended_action") if decision else None,
        final_status=decision.get("final_status") if decision else None,
        autonomous=decision.get("autonomous") if decision else None,
        human_override=bool(decision.get("human_override", False)) if decision else False,
        score_value=score.get("value") if score else None,
        score_lower=bounds.get("lower"),
        score_upper=bounds.get("upper"),
        threshold_qualify=score.get("threshold_qualify") if score else None,
        threshold_reject=score.get("threshold_reject") if score else None,
        confidence=score.get("confidence") if score else None,
        tau=score.get("tau") if score else None,
        stop_reason=stopping.get("reason_code") if stopping else None,
        stop_explanation=stopping.get("explanation") if stopping else None,
        provider_cost_usd=str(cost.get("provider_cost_usd", "0")),
        model_cost_usd=str(cost.get("model_cost_usd", "0")),
        total_cost_usd=str(cost.get("total_cost_usd", "0")),
        evidence_known=tuple(item["field"] for item in evidence.get("items") or ()),
        evidence_unknown=tuple(evidence.get("unknown_fields") or ()),
        activity=split_provider_activity(receipt),
        policy_version=versions.get("policy") if versions else None,
        scorer_version=versions.get("scorer") if versions else None,
        confidence_calibration=versions.get("confidence_calibration") if versions else None,
        human_review=_human_review(receipt),
    )
