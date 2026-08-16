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
Step 8) — a linear scaffold connecting ``NEW`` through ``DECISION``. It is
*not* a reimplementation of ``CalibratedBoundsPolicy``'s actual adaptive
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

# No job_type_for entry -- and therefore no automatic advancement -- for
# AUTO_ROUTED, AWAITING_HUMAN, or MANUAL_REVIEW: routing/CRM sync and human
# review are both not built yet (n8n edge workflows, human approval path).
# ROUTED has nowhere further to go without that sync path either.
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

    Raises ValueError for DECISION without a recognised outcome — silently
    guessing which way a decision branched would be worse than failing loudly.
    Returns None for terminal states and for any status this module doesn't
    yet auto-advance (see the module docstring).
    """
    if current is LeadStatus.DECISION:
        if outcome not in DECISION_OUTCOMES:
            raise ValueError(
                f"DECISION requires one of {sorted(DECISION_OUTCOMES)} as outcome, got {outcome!r}"
            )
        return DECISION_OUTCOMES[outcome]

    if current in TERMINAL:
        return None

    transition = _TRANSITIONS.get(current)
    return transition.on_success if transition is not None else None
