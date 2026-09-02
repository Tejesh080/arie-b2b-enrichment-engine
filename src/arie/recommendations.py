"""What ARIE tells a customer to do about one lead — the payoff M7 Slice 4
exists to deliver.

Every previous M7 slice, and M1-M6 before it, produce a *machine* answer: a
``Decision``, a calibrated ``confidence``, a lead status, a human-review
verdict. None of that is what a customer opens the product to read. This
module is the deterministic, LLM-free translation from "what ARIE decided"
to "what a person should do next" — see ``arie.intelligence.explanation`` for
the one part of the customer-facing surface that *is* allowed to call a model
(the prose explaining *why*, never the classification itself).

**Deterministic by construction, not by convention.** Every derivation below
is a pure function of :class:`DecisionSignal` — a small, primitive-typed
summary of a decision. No model call, no randomness, no provider I/O. The
same signal always produces the same :class:`LeadRecommendation`, which is
what lets the explanation layer, the feedback layer, and every test in
``tests/unit/test_recommendations.py`` treat "priority" and "next action" as
facts rather than as something to double-check.

**Two ways to build a `DecisionSignal`, one set of rules.**
:func:`DecisionSignal.from_receipt` reads a full
:class:`~arie.api.receipt.DecisionReceipt` (`GET /leads/{lead_id}/receipt`
already builds one; computing a recommendation from it adds no new
database read). A batch results *list*, showing many leads at once, cannot
afford a full receipt per row — see ``arie.batches``'s own bulk query — so it
builds a `DecisionSignal` directly from a lighter joined query instead. Both
paths run through the exact same `derive_customer_priority`/
`derive_next_action`, so a lead's priority never depends on which endpoint
asked.

**The unknown-vs-negative invariant carries over.** ``arie.scoring.rules``
never treats an unobserved field as a disqualifying one; this module makes the
identical promise about priority: a lead ARIE has not finished evidencing is
routed to :attr:`CustomerPriority.REVIEW`, never silently downgraded to
:attr:`CustomerPriority.SKIP` on the strength of an absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from arie.api.receipt import DecisionReceipt
from arie.core.types import Decision, LeadStatus
from arie.statemachine.transitions import AWAITING_REVIEW, FAILURE, REJECTED

__all__ = [
    "ConfidenceBand",
    "CustomerPriority",
    "DecisionSignal",
    "LeadRecommendation",
    "NextAction",
    "ResearchStatus",
    "build_recommendation",
    "confidence_band",
    "derive_customer_priority",
    "derive_next_action",
    "derive_research_status",
]


class CustomerPriority(StrEnum):
    """The only classification a customer sees on a batch results list.

    Deliberately four values, in descending urgency, and never produced by a
    model — see :func:`derive_customer_priority`.
    """

    CONTACT_FIRST = "contact_first"
    WORTH_PURSUING = "worth_pursuing"
    REVIEW = "review"
    SKIP = "skip"


class NextAction(StrEnum):
    """What a customer should physically do next. Structural, not a paid
    research authorization — see :func:`derive_next_action` for why
    ``RESEARCH_MORE`` here never triggers a provider call."""

    CONTACT_NOW = "contact_now"
    EMAIL_FIRST = "email_first"
    FIND_DECISION_MAKER = "find_decision_maker"
    RESEARCH_MORE = "research_more"
    NURTURE = "nurture"
    SKIP = "skip"
    HUMAN_REVIEW = "human_review"


class ConfidenceBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ResearchStatus(StrEnum):
    NOT_NEEDED = "not_needed"
    NOT_PERFORMED = "not_performed"
    RESEARCHED = "researched"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


_HIGH_CONFIDENCE = 0.75
_MEDIUM_CONFIDENCE = 0.45
"""Fixed, deterministic bands. Not sourced from `arie.confidence`'s own
calibration thresholds (`tau`) — those decide whether ARIE acts autonomously,
a different question from how to describe a number to a customer, and tying
the two would make this module's output move every time an organization's
policy tuning changed, which is not what a "how sure are we" label should do."""

# Human-readable labels for the field-name vocabulary
# `arie.scoring.rules.SCORED_FIELDS` uses in a receipt's `evidence.items` /
# `unknown_fields`. This is the only place that maps ARIE's internal field
# names to customer language — every deterministic sentence in this module
# reads through it rather than restating labels inline.
FIELD_LABELS: dict[str, str] = {
    "employee_count": "company size",
    "industry": "industry",
    "title_seniority": "contact seniority",
    "title_function": "contact function",
    "buying_intent": "buying intent",
    "recent_trigger_event": "a recent trigger event",
    "disqualifying_flag": "a disqualifying condition",
}

_CONTACT_SENIORITY_FIELD = "title_seniority"
"""Whether this field is *known* (not in `unknown_fields`) is this module's
proxy for "ARIE has identified who to talk to" — see `derive_next_action`.
Not the *value* (e.g. distinguishing a VP from an individual contributor):
a decision receipt never carries field values, only which field resolved and
from where, and re-fetching the shared, mutable `evidence` table here to read
one field's value would reintroduce exactly the point-in-time inconsistency
`arie.api.receipt`'s own module docstring explains `decision_receipts` exists
to avoid."""


def confidence_band(confidence: float) -> ConfidenceBand:
    if confidence >= _HIGH_CONFIDENCE:
        return ConfidenceBand.HIGH
    if confidence >= _MEDIUM_CONFIDENCE:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


@dataclass(frozen=True)
class DecisionSignal:
    """The minimal, primitive-typed input every derivation in this module
    actually needs. Two constructors produce one: a full lead detail view
    reads it off a `DecisionReceipt` (:meth:`from_receipt`); a batch results
    list reads it off one joined SQL row (`arie.batches`) without paying for
    a full receipt per row. Everything below this type is pure and knows
    nothing about where its inputs came from.
    """

    decided: bool
    """`False` for a lead still mid-pipeline ("pending") or dead-lettered
    before a decision ("processing_failed") — see `DecisionReceipt.status`."""
    lead_status: LeadStatus
    recommended_action: str | None
    """`Decision` enum value, or `None` before a decision exists."""
    confidence: float | None
    score: float | None
    known_fields: tuple[str, ...]
    """Field names `arie.scoring.rules.SCORED_FIELDS` resolved by decision
    time — `decision_receipts.evidence_snapshot["known"]`'s field names."""
    unknown_fields: tuple[str, ...]
    profile_version: int | None
    shadow: bool
    execution_mode: str | None
    research_status: ResearchStatus | None
    """`None` when this signal's source did not fetch provider-call history
    (a batch list row) — see `LeadRecommendation.research_status`'s own note.
    Always populated for a signal built from a full receipt."""

    @classmethod
    def from_receipt(cls, receipt: DecisionReceipt) -> DecisionSignal:
        return cls(
            decided=receipt.status == "decided",
            lead_status=receipt.lead_status,
            recommended_action=receipt.decision.recommended_action if receipt.decision else None,
            confidence=receipt.score.confidence if receipt.score is not None else None,
            score=receipt.score.value if receipt.score is not None else None,
            known_fields=tuple(
                item.field for item in receipt.evidence.items if item.field != "disqualifying_flag"
            ),
            unknown_fields=receipt.evidence.unknown_fields,
            profile_version=receipt.versions.icp_profile_version if receipt.versions else None,
            shadow=receipt.shadow,
            execution_mode=receipt.execution_mode,
            research_status=_research_status_from_calls(receipt),
        )

    @classmethod
    def from_decision_row(
        cls,
        *,
        lead_status: LeadStatus,
        shadow: bool,
        decision: str | None,
        confidence: float | None,
        score_value: float | None,
        evidence_snapshot: dict[str, Any] | None,
        profile_version: int | None,
    ) -> DecisionSignal:
        """Build a signal from one `decision_receipts` row joined straight into
        a bulk listing query (`arie.batches.list_batch_rows`) — the same
        `evidence_snapshot` JSON shape `arie.api.receipt._evidence_items`
        parses, read here without a full `DecisionReceipt` per row.

        `research_status` is always `None` here: a batch list does not also
        join `provider_calls` per row (see `arie.batches`'s own comment on
        why), so this signal honestly says "not computed" rather than
        guessing. `GET /leads/{lead_id}/recommendation` uses
        :meth:`from_receipt` instead, which always knows.
        """
        snapshot: dict[str, Any] = evidence_snapshot or {}
        known = tuple(
            entry["field"]
            for entry in snapshot.get("known", [])
            if entry.get("field") != "disqualifying_flag"
        )
        unknown: tuple[str, ...] = tuple(snapshot.get("unknown", ()))
        return cls(
            decided=decision is not None,
            lead_status=lead_status,
            recommended_action=decision,
            confidence=confidence,
            score=score_value,
            known_fields=known,
            unknown_fields=unknown,
            profile_version=profile_version,
            shadow=shadow,
            execution_mode=snapshot.get("execution_mode"),
            research_status=None,
        )


def _research_status_from_calls(receipt: DecisionReceipt) -> ResearchStatus:
    """Never claims "researched" for a call this organization's execution mode
    marks as simulated — the caller (``LeadRecommendation.execution_mode``)
    still carries that distinction so a UI can label it, per the M5
    truthfulness rule this module inherits rather than restates."""
    if receipt.status != "decided":
        return ResearchStatus.NOT_PERFORMED
    calls = receipt.providers.called
    if not calls:
        return ResearchStatus.NOT_PERFORMED
    successful = [c for c in calls if c.status == "success"]
    if not successful:
        return ResearchStatus.UNAVAILABLE
    if len(successful) < len(calls):
        return ResearchStatus.PARTIAL
    return ResearchStatus.RESEARCHED


def derive_customer_priority(signal: DecisionSignal) -> CustomerPriority:
    """The customer-facing classification for one lead. Pure; no LLM.

    Reads only `signal` — no confidence threshold here is a model's opinion,
    and no branch below depends on which (if any) AI provider is configured.
    """
    if not signal.decided:
        # Still mid-pipeline, or broke before deciding. Neither is a business
        # decision yet, so this is uncertainty, not a rejection — the same
        # unknown-vs-negative rule `arie.scoring.rules` applies to a single
        # field applies here to the lead as a whole.
        return CustomerPriority.REVIEW

    status = signal.lead_status
    if status in FAILURE or status in AWAITING_REVIEW:
        return CustomerPriority.REVIEW
    if status in REJECTED:
        return CustomerPriority.SKIP

    # QUALIFIED (AUTO_ROUTED / ROUTED / MANUAL_REVIEW) or a shadow evaluation
    # that never opened a real review — both carry a real decision/score.
    assert signal.recommended_action is not None and signal.confidence is not None
    strong_autonomous_fit = (
        signal.recommended_action == str(Decision.AUTO_ROUTE)
        and confidence_band(signal.confidence) is ConfidenceBand.HIGH
    )
    return (
        CustomerPriority.CONTACT_FIRST if strong_autonomous_fit else CustomerPriority.WORTH_PURSUING
    )


def derive_next_action(priority: CustomerPriority, signal: DecisionSignal) -> NextAction:
    """What a customer should do next. Pure; no LLM, no provider call.

    `RESEARCH_MORE` is a customer-facing suggestion only — it never itself
    authorizes or triggers a paid provider call. That authorization is a
    later slice's `arie.live` acquisition policy, which this module never
    reaches into.
    """
    if not signal.decided:
        return NextAction.RESEARCH_MORE

    status = signal.lead_status
    if status in FAILURE:
        return NextAction.HUMAN_REVIEW
    if status in AWAITING_REVIEW:
        return NextAction.HUMAN_REVIEW
    if priority is CustomerPriority.SKIP:
        return NextAction.SKIP

    has_decision_maker_contact = _CONTACT_SENIORITY_FIELD not in signal.unknown_fields
    if priority is CustomerPriority.CONTACT_FIRST:
        return (
            NextAction.CONTACT_NOW if has_decision_maker_contact else NextAction.FIND_DECISION_MAKER
        )
    if priority is CustomerPriority.WORTH_PURSUING:
        if not has_decision_maker_contact:
            return NextAction.FIND_DECISION_MAKER
        return NextAction.EMAIL_FIRST if signal.unknown_fields else NextAction.NURTURE

    # priority is REVIEW for a reason other than an open human review or a
    # pipeline failure (both handled above) — genuine scoring uncertainty.
    return NextAction.RESEARCH_MORE


def derive_research_status(signal: DecisionSignal) -> ResearchStatus:
    """`signal.research_status` if the caller computed it from provider-call
    history, else `NOT_PERFORMED` — never a claim that research happened
    when this signal's source (a batch list row) never checked."""
    return (
        signal.research_status
        if signal.research_status is not None
        else ResearchStatus.NOT_PERFORMED
    )


def _known_field_labels(signal: DecisionSignal) -> list[str]:
    return [FIELD_LABELS.get(field, field) for field in signal.known_fields]


def _missing_field_labels(signal: DecisionSignal) -> list[str]:
    return [FIELD_LABELS.get(field, field) for field in signal.unknown_fields]


def _deterministic_short_reason(
    priority: CustomerPriority, signal: DecisionSignal, known: list[str], missing: list[str]
) -> str:
    """A short, honest sentence built only from field labels — never a value,
    never a number. This is `LeadRecommendation.short_reason`, always
    present with no AI call; see `arie.intelligence.explanation` for the
    richer, evidence-cited version a customer can request on demand."""
    if not signal.decided:
        return "ARIE is still gathering evidence on this lead."
    status = signal.lead_status
    if status in FAILURE:
        return "Processing hit a problem before ARIE could finish evaluating this lead."
    if status in AWAITING_REVIEW:
        return "This lead is waiting on a human review before it can move forward."
    if priority is CustomerPriority.SKIP:
        return "This lead falls outside your targeting profile."

    strength = "Strong match" if priority is CustomerPriority.CONTACT_FIRST else "Possible match"
    if known:
        sentence = f"{strength} based on {', '.join(known[:3])}."
    else:
        sentence = f"{strength}, though little evidence has been gathered yet."
    if missing:
        sentence += f" {missing[0].capitalize()} is still unknown."
    return sentence


@dataclass(frozen=True)
class LeadRecommendation:
    """The customer-facing surface for one lead — see the M7 Slice 4 handoff's
    "Part C — recommendation domain". Everything here is derived, never
    stored: a fresh signal always produces a fresh, consistent recommendation."""

    lead_id: UUID
    priority: CustomerPriority
    next_action: NextAction
    machine_decision: str | None
    """`Decision` enum value ARIE's policy concluded, or `None` before a
    decision exists — Advanced Details territory, kept for a caller that
    wants to cross-reference the Decision Receipt."""
    score: float | None
    confidence: float | None
    confidence_band: ConfidenceBand | None
    short_reason: str
    key_evidence: list[str]
    missing_information: list[str]
    research_status: ResearchStatus
    explanation_status: str
    """`"not_requested"` — the deterministic reason above is all that has been
    computed; a caller gets AI prose only by asking
    `arie.intelligence.explanation` for it separately (Part F's cost
    discipline: no LLM call is made just to answer this endpoint)."""
    profile_version: int | None
    shadow: bool
    execution_mode: str | None

    @property
    def is_decided(self) -> bool:
        return self.score is not None


def build_recommendation(lead_id: UUID, signal: DecisionSignal) -> LeadRecommendation:
    """Assemble the full customer-facing recommendation for one lead from a
    `DecisionSignal` — no database read and no LLM call of its own; the
    signal already paid for whichever query built it.
    """
    priority = derive_customer_priority(signal)
    next_action = derive_next_action(priority, signal)
    known = _known_field_labels(signal)
    missing = _missing_field_labels(signal)
    return LeadRecommendation(
        lead_id=lead_id,
        priority=priority,
        next_action=next_action,
        machine_decision=signal.recommended_action,
        score=signal.score,
        confidence=signal.confidence,
        confidence_band=confidence_band(signal.confidence)
        if signal.confidence is not None
        else None,
        short_reason=_deterministic_short_reason(priority, signal, known, missing),
        key_evidence=known,
        missing_information=missing,
        research_status=derive_research_status(signal),
        explanation_status="not_requested",
        profile_version=signal.profile_version,
        shadow=signal.shadow,
        execution_mode=signal.execution_mode,
    )


def score_snapshot(recommendation: LeadRecommendation) -> Decimal | None:
    """`LeadRecommendation.score`, as a `Decimal` — for `arie.feedback`'s
    snapshot column, which is `NUMERIC` rather than `float`."""
    return Decimal(str(recommendation.score)) if recommendation.score is not None else None
