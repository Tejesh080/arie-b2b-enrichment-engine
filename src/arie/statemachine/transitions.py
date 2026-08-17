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
``LeadStatus`` label can't carry. That loop runs in ``arie.jobs.handlers``'
one ``compute_score`` handler, which walks a lead through this scaffold's
statuses itself rather than splitting the calculation across four job types —
see that module's docstring for the argument. What this module guarantees is
that the graph, however it is driven, transitions safely and atomically; see
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

# --- business-semantic status groups ----------------------------------------
#
# A *different axis* from TERMINAL above, which is about whether this
# module's job-queue mechanism auto-advances a status -- a mechanical
# property. These are about what a status *means* to a downstream consumer
# (a cost metric, an outcome-sync webhook): AUTO_ROUTED and MANUAL_REVIEW are
# both business-finalized outcomes but neither is in TERMINAL, because
# nothing auto-advances either one yet either way (see TERMINAL's own
# comment) -- "finalized" and "the graph won't move this further on its own"
# are related but not the same claim, and conflating them is exactly how
# three different, silently-drifting definitions of "finalized" (this
# module's TERMINAL, the n8n outcome-sync gate, and the CI smoke test) ended
# up disagreeing with each other and with themselves. These groups are the
# one place that vocabulary is defined; everything else -- SQL views, n8n's
# JSON, CI -- must be read off these rather than restating the status list.

QUALIFIED: frozenset[LeadStatus] = frozenset(
    {
        LeadStatus.AUTO_ROUTED,
        LeadStatus.ROUTED,
        LeadStatus.MANUAL_REVIEW,
    }
)
"""The policy or a human decided this lead was worth routing onward --
`v_pipeline_metrics.cost_per_qualified_lead`'s actual numerator. Deliberately
excludes SYNCED: that status is `DECISION_OUTCOMES`/`HUMAN_REVIEW_OUTCOMES`'s
own *reject* terminal (see their comments above), not a qualified lead that
happened to finish syncing. A metric counting SYNCED as qualified rewards
rejecting more leads -- the exact inversion 0005 already fixed once for the
automatic-reject path; MANUAL_REVIEW being excluded from the *old* filter
was the same bug on the human-review path, just never named as such."""

REJECTED: frozenset[LeadStatus] = frozenset({LeadStatus.SYNCED})
"""The reject branch's terminal, named for what it means rather than for
what the status happens to be called -- both the automatic reject
(DECISION_OUTCOMES) and a human's reject (HUMAN_REVIEW_OUTCOMES) land here."""

AWAITING_REVIEW: frozenset[LeadStatus] = frozenset({LeadStatus.AWAITING_HUMAN})
"""Paused on a human, not finalized -- distinct from both QUALIFIED/REJECTED
(ARIE finished deciding) and FAILURE (ARIE broke). A downstream consumer
polling for "is this lead done" must keep waiting on this one, not treat it
as either success or failure."""

FAILURE: frozenset[LeadStatus] = frozenset({LeadStatus.FAILED, LeadStatus.DEAD_LETTER})
"""Processing broke, permanently -- not a business decision at all. A
downstream consumer polling for "is this lead done" must be told this
explicitly rather than being left in a "not finalized yet" loop forever,
since nothing further happens to these leads without manual intervention."""

FINALIZED: frozenset[LeadStatus] = QUALIFIED | REJECTED
"""Every status ARIE's own decisioning (policy or human review) has finished
making a call on. This is "finalized" from a downstream consumer's point of
view -- e.g. what `workflows/n8n/outcome-sync.json`'s own gate means, and
what `tests/unit/test_n8n_workflows.py` checks that gate's literal status
list against, so the two can't silently drift apart again."""


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
