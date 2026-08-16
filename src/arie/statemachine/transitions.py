"""The lead state graph — pure, DB-free.

``arie.core.types.LeadStatus`` says "transitions live in arie.statemachine";
this is that. Two functions, deliberately kept separate:

- ``job_type_for(status)`` — what work advances a lead currently in this
  status. ``None`` means the queue doesn't auto-advance past it (either it's
  terminal, or advancing it needs something this module doesn't own — a human
  decision, an external sync).
- ``next_status(current, outcome=...)`` — what status a lead moves to once
  that work succeeds.

**Scope, stated plainly:** this is the queue/state-machine *mechanism* (M1
Step 8, extended in Step 11) — a linear scaffold connecting ``NEW`` through
``DECISION``, plus the one additional branch a human review resolves
(``AWAITING_HUMAN`` -> outcome). It is *not* a reimplementation of
``CalibratedBoundsPolicy``'s actual adaptive
score/fetch-evidence loop, which decides whether to keep buying evidence from
bounds and confidence computed over evidence *content* — information a bare
``LeadStatus`` label can't carry, and real provider adapters this module has
no dependency on. Wiring real handlers (scoring, evidence fetching, the
policy itself) behind ``SCORING``/``FETCHING_EVIDENCE``/``INTEGRATING`` is
future work, deliberately deferred — see docs/06-m1-handoff.md's suggested
order, item 5. What this module guarantees today is that the graph, however
it ends up populated, transitions safely and atomically; see
``arie.statemachine.apply``.
"""

from __future__ import annotations

from dataclasses import dataclass

from arie.core.types import LeadStatus


@dataclass(frozen=True)
class Transition:
    job_type: str
    on_success: LeadStatus


# The auto-advancing portion of the graph. Everything reachable from NEW
# through DECISION without external input.
_TRANSITIONS: dict[LeadStatus, Transition] = {
    LeadStatus.NEW: Transition("compute_score", LeadStatus.SCORING),
    LeadStatus.SCORING: Transition("fetch_evidence", LeadStatus.FETCHING_EVIDENCE),
    LeadStatus.FETCHING_EVIDENCE: Transition("integrate_evidence", LeadStatus.INTEGRATING),
    LeadStatus.INTEGRATING: Transition("finalize_decision", LeadStatus.DECISION),
}

# DECISION is the one node that can't be a bare current-status -> next-status
# mapping honestly: which branch is taken depends on the job's own result, not
# the status label. The caller (the handler that ran the "finalize_decision"
# job) passes back which of these happened.
DECISION_OUTCOMES: dict[str, LeadStatus] = {
    "auto_route": LeadStatus.AUTO_ROUTED,
    "escalate_human": LeadStatus.AWAITING_HUMAN,
    "reject": LeadStatus.SYNCED,  # nothing further to route
}

# AWAITING_HUMAN is the same shape of branch as DECISION, one level later: which
# status a reviewed lead moves to depends on what the human decided, not on the
# status label. Keyed by the decision-label vocabulary `human_reviews.
# final_decision` already uses (see the hand-written fixtures in
# test_cost_ledger_integration.py predating this step) -- not by the review
# "action" a caller submits (approve/reject/edit, arie.approval.workflow
# .ReviewAction), which is a human-facing verb translated into this vocabulary
# before it ever reaches the state graph. "reject" intentionally reuses
# DECISION_OUTCOMES's own SYNCED target: a human rejecting a lead terminates it
# the same way the automatic reject branch does, and 0005's comment about SYNCED
# being an ambiguous, deliberately-deferred terminal applies identically here.
# "manual_review" is the one outcome with no automatic-path equivalent -- a
# human materially overriding the decision rather than a plain approve/reject,
# landing on MANUAL_REVIEW rather than being forced into one of the other two.
HUMAN_REVIEW_OUTCOMES: dict[str, LeadStatus] = {
    "auto_route": LeadStatus.AUTO_ROUTED,
    "reject": LeadStatus.SYNCED,
    "manual_review": LeadStatus.MANUAL_REVIEW,
}

# No job_type_for entry -- and therefore no automatic advancement -- for
# AUTO_ROUTED or MANUAL_REVIEW: routing/CRM sync isn't built yet (n8n edge
# workflows), and MANUAL_REVIEW is a resting state for an overridden lead with
# no further auto-advancement defined. AWAITING_HUMAN is no longer in this
# category as of M1 Step 11 -- see HUMAN_REVIEW_OUTCOMES and next_status below.
# ROUTED has nowhere further to go without the sync path either.
TERMINAL: frozenset[LeadStatus] = frozenset(
    {
        LeadStatus.ROUTED,
        LeadStatus.SYNCED,
        LeadStatus.FAILED,
        LeadStatus.DEAD_LETTER,
    }
)


def job_type_for(status: LeadStatus) -> str | None:
    """The job type that advances a lead out of `status`, or None if nothing does yet."""
    transition = _TRANSITIONS.get(status)
    return transition.job_type if transition is not None else None


def next_status(current: LeadStatus, *, outcome: str | None = None) -> LeadStatus | None:
    """The status a lead moves to once the work for `current` succeeds.

    Raises ValueError for DECISION or AWAITING_HUMAN without a recognised
    outcome — silently guessing which way a decision or a review branched
    would be worse than failing loudly. Returns None for terminal states and
    for any status this module doesn't yet auto-advance (see the module
    docstring).
    """
    if current is LeadStatus.DECISION:
        if outcome not in DECISION_OUTCOMES:
            raise ValueError(
                f"DECISION requires one of {sorted(DECISION_OUTCOMES)} as outcome, got {outcome!r}"
            )
        return DECISION_OUTCOMES[outcome]

    if current is LeadStatus.AWAITING_HUMAN:
        if outcome not in HUMAN_REVIEW_OUTCOMES:
            raise ValueError(
                f"AWAITING_HUMAN requires one of {sorted(HUMAN_REVIEW_OUTCOMES)} "
                f"as outcome, got {outcome!r}"
            )
        return HUMAN_REVIEW_OUTCOMES[outcome]

    if current in TERMINAL:
        return None

    transition = _TRANSITIONS.get(current)
    return transition.on_success if transition is not None else None
